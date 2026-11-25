# Copyright (c) 2026, Techlift Technologies and contributors
# For license information, please see license.txt

"""Bulk update Item "Display Order In Listing" from an Excel file.

Every row marked Action Needed = Yes is saved through the full Item save, even when the
display order is already the same: the point of the row is to fire the Item webhooks and
push the product to the websites again. After each Item is saved, one of its variants gets
Last Pinged On set to today and is saved too, so the variant webhooks fire once per template
(see ping_variant). Rows marked No are skipped, and so are rows whose
Item does not exist (counted and listed, not failed) - the same as the Item Supplier Import Tool.

Saving thousands of Items is far too slow for one request or one job, so submit splits the
rows into chunks and queues one background job per chunk. Each chunk adds its own counts
and errors to this document, and the last one to finish sets the final status.
"""

import os

import frappe
from frappe.model.document import Document
from frappe.utils.xlsxutils import read_xls_file_from_attached_file, read_xlsx_file_from_attached_file
from frappe.utils.file_manager import save_file

FIELD = "nat_display_order_in_listing"
REQUIRED_HEADERS = ["Item Code", "Display Order", "Action Needed"]

# Each Item save can also re-save up to 30 variants inline, so keep chunks small enough
# that a slow one still finishes well inside the job timeout.
CHUNK_SIZE = 100
CHUNK_TIMEOUT = 3600


class ItemDisplayOrderImportTool(Document):
	def validate(self):
		rows, skipped = parse_rows(read_excel(self.excel_file))
		self.total_rows = len(rows) + skipped
		self.rows_to_update = len(rows)
		self.rows_skipped = skipped

		if not rows:
			frappe.throw("No rows have Action Needed = Yes, so there is nothing to update.")

		missing = missing_items([r["item_code"] for r in rows])
		self.rows_to_update = len(rows) - len(missing)
		self.rows_not_found = len(missing)
		self.not_found_log = "\n".join(missing)
		# validate runs again on submit; the warning has been shown by then
		if missing and self._action != "submit":
			sample = ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")
			frappe.msgprint(
				f"{len(missing)} item(s) marked Yes were not found and will be skipped: {sample}",
				title="Items not found",
				indicator="orange",
			)
		if not self.rows_to_update:
			frappe.throw("None of the items marked Yes exist, so there is nothing to update.")

	def on_submit(self):
		rows, _skipped = parse_rows(read_excel(self.excel_file))
		# Checked again here, not taken from validate: an item can be deleted in between.
		missing = set(missing_items([r["item_code"] for r in rows]))
		rows = [r for r in rows if r["item_code"] not in missing]
		self.db_set({"status": "Queued", "updated": 0, "failed": 0, "variants_pinged": 0, "error_log": "",
			"rows_to_update": len(rows), "rows_not_found": len(missing),
			"not_found_log": "\n".join(sorted(missing))})
		if not rows:
			# nothing to queue, so no chunk would ever set the final status
			self.db_set("status", "Completed")
			frappe.msgprint("None of the items marked Yes exist, so nothing was updated.")
			return

		for start in range(0, len(rows), CHUNK_SIZE):
			frappe.enqueue(
				"metactical.metactical.doctype.item_display_order_import_tool."
				"item_display_order_import_tool.process_chunk",
				queue="long",
				timeout=CHUNK_TIMEOUT,
				enqueue_after_commit=True,
				docname=self.name,
				rows=rows[start:start + CHUNK_SIZE],
			)

		frappe.msgprint(
			f"Queued {len(rows)} item(s) in {-(-len(rows) // CHUNK_SIZE)} background job(s). "
			"Refresh this page to follow the progress.",
			title="Import queued",
		)

	def on_cancel(self):
		if self.status in ("Queued", "In Progress"):
			frappe.throw("This import is still running. Wait for it to finish before cancelling.")


@frappe.whitelist(methods=["POST"])
def import_display_order():
	"""Upload a display order file and start the import, in one call - like import_item_supplier.

	POST multipart/form-data with the Excel file as `file`. The file is checked, saved on a new
	Item Display Order Import Tool and submitted straight away: reading even a 50k-row file takes
	seconds, and submit only queues the background jobs that save the Items. Throws, and creates
	nothing, when the file has a missing header or an invalid row.
	"""
	# who may run it is the doctype's own permissions (create + submit), checked by insert/submit
	uploaded_file = frappe.request.files.get("file")
	if not uploaded_file:
		frappe.throw("No file received")

	file_doc = save_file(fname=uploaded_file.filename, content=uploaded_file.read(),
		dt=None, dn=None, is_private=True)

	doc = frappe.get_doc({"doctype": "Item Display Order Import Tool", "excel_file": file_doc.file_url})
	doc.insert()

	# attach the file to its import, so it is found from the document and deleted with it
	file_doc.reload()
	file_doc.attached_to_doctype = doc.doctype
	file_doc.attached_to_name = doc.name
	file_doc.save()

	doc.submit()
	frappe.db.commit()
	doc.reload()

	return {
		"status": "success",
		"queue_status": doc.status,
		"file_url": file_doc.file_url,
		"docname": doc.name,
		"total_rows": doc.total_rows,
		"rows_to_update": doc.rows_to_update,
		"rows_skipped": doc.rows_skipped,
		"rows_not_found": doc.rows_not_found,
		"missing_items": [{"item not found": code} for code in (doc.not_found_log or "").split("\n") if code],
	}


def read_excel(file_url):
	if not file_url:
		frappe.throw("Attach an Excel file first.")

	extn = os.path.splitext(file_url)[1][1:].lower()
	file_name = frappe.db.get_value("File", {"file_url": file_url})
	if not file_name:
		frappe.throw(f"Could not find the attached file {file_url}.")
	content = frappe.get_doc("File", file_name).get_content()

	if extn == "xlsx":
		return read_xlsx_file_from_attached_file(fcontent=content)
	if extn == "xls":
		return read_xls_file_from_attached_file(content)
	frappe.throw("Only xls and xlsx files are supported.")


def parse_rows(data):
	"""Return (rows to update, number skipped). Throws on anything that can't be imported."""
	if not data:
		frappe.throw("The Excel file is empty.")

	columns = {}
	for idx, header in enumerate(data[0]):
		header = str(header).strip() if header is not None else ""
		if header and header not in columns:
			columns[header] = idx
	missing = [h for h in REQUIRED_HEADERS if h not in columns]
	if missing:
		frappe.throw(f"Missing required header(s) in this Excel file: {', '.join(missing)}")

	def cell(row, header):
		idx = columns[header]
		return row[idx] if idx < len(row) else None

	rows, skipped, errors, seen = [], 0, [], {}
	for row_no, row in enumerate(data[1:], start=2):
		if not any(v not in (None, "") for v in row):
			continue  # blank line

		action = str(cell(row, "Action Needed") or "").strip().lower()
		if action in ("", "no"):
			skipped += 1
			continue
		if action != "yes":
			errors.append(f"Row {row_no}: Action Needed must be Yes or No, got '{cell(row, 'Action Needed')}'")
			continue

		item_code = str(cell(row, "Item Code") or "").strip()
		if not item_code:
			errors.append(f"Row {row_no}: Item Code is empty")
			continue

		value = cell(row, "Display Order")
		try:
			display_order = int(float(str(value).strip()))
		except (TypeError, ValueError):
			errors.append(f"Row {row_no}: Display Order '{value}' for {item_code} is not a number")
			continue

		if item_code in seen:
			errors.append(f"Row {row_no}: {item_code} is already marked Yes on row {seen[item_code]}")
			continue
		seen[item_code] = row_no

		rows.append({"row_no": row_no, "item_code": item_code, "display_order": display_order})

	if errors:
		more = f"<br>... and {len(errors) - 30} more" if len(errors) > 30 else ""
		frappe.throw("Fix these rows and attach the file again:<br>" + "<br>".join(errors[:30]) + more,
			title="Invalid rows")

	return rows, skipped


def missing_items(item_codes):
	found = set()
	for start in range(0, len(item_codes), 1000):
		batch = item_codes[start:start + 1000]
		found.update(frappe.get_all("Item", filters={"name": ["in", batch]}, pluck="name"))
	return [code for code in item_codes if code not in found]


def process_chunk(docname, rows):
	"""Save each Item in the chunk with its new display order. Runs as the user who submitted."""
	frappe.db.set_value("Item Display Order Import Tool", docname, "status", "In Progress",
		update_modified=False)
	frappe.db.commit()

	updated, pinged, not_found, errors = 0, 0, [], []
	for row in rows:
		# error_text reads the last message: start each row clean so it can't pick up one
		# left over from an earlier row or from the request that queued this job.
		frappe.local.message_log = []
		try:
			if not frappe.db.exists("Item", row["item_code"]):
				# deleted since the import was submitted: skipped, like the ones found then
				not_found.append(row["item_code"])
				continue

			item = frappe.get_doc("Item", row["item_code"])
			item.set(FIELD, row["display_order"])
			# A full save even when the value is unchanged: on_update fires the Item webhooks.
			item.save()
			frappe.db.commit()  # webhooks are queued on commit
			updated += 1
		except Exception as e:
			frappe.db.rollback()
			errors.append(f"Row {row['row_no']} ({row['item_code']}): {error_text(e)}")
			continue
		finally:
			frappe.local.message_log = []

		# The template is already saved and counted: a failed ping only adds a line to the log.
		try:
			if ping_variant(row["item_code"]):
				frappe.db.commit()
				pinged += 1
		except Exception as e:
			frappe.db.rollback()
			errors.append(f"Row {row['row_no']} ({row['item_code']}): updated, but pinging a variant failed: {error_text(e)}")
		finally:
			frappe.local.message_log = []

	record_chunk(docname, updated, errors, not_found, pinged)


def ping_variant(template):
	"""Set Last Pinged On to today on one variant of the template and save it, which fires its
	webhooks. One variant per template is enough. Prefers an enabled variant; returns the variant
	saved, or None when the item has no variants (a plain item)."""
	variants = frappe.get_all("Item", filters={"variant_of": template},
		order_by="disabled asc, name asc", limit=1, pluck="name")
	if not variants:
		return None

	variant = frappe.get_doc("Item", variants[0])
	variant.last_pinged_on = frappe.utils.today()
	variant.save()
	return variant.name


def error_text(e):
	if frappe.local.message_log:
		try:
			return frappe.utils.strip_html(frappe.parse_json(frappe.local.message_log[-1]).get("message", ""))[:300]
		except Exception:
			pass
	return (str(e) or type(e).__name__)[:300]


def record_chunk(docname, updated, errors, not_found=(), pinged=0):
	# One atomic UPDATE per chunk: chunks can run in parallel on several workers. An item
	# deleted since submit moves from Rows To Update to Skipped (Item Not Found).
	frappe.db.sql(
		"""update `tabItem Display Order Import Tool`
		set updated = ifnull(updated, 0) + %s,
			variants_pinged = ifnull(variants_pinged, 0) + %s,
			failed = ifnull(failed, 0) + %s,
			error_log = concat(ifnull(error_log, ''), %s),
			rows_to_update = ifnull(rows_to_update, 0) - %s,
			rows_not_found = ifnull(rows_not_found, 0) + %s,
			not_found_log = concat(ifnull(not_found_log, ''), %s)
		where name = %s""",
		(updated, pinged, len(errors), "".join(line + "\n" for line in errors),
		 len(not_found), len(not_found), "".join("\n" + code for code in not_found), docname),
	)
	frappe.db.commit()

	doc = frappe.db.get_value("Item Display Order Import Tool", docname,
		["rows_to_update", "updated", "failed", "rows_not_found", "variants_pinged"], as_dict=True)
	if (doc.updated or 0) + (doc.failed or 0) >= (doc.rows_to_update or 0):
		status = "Completed with Errors" if doc.failed else "Completed"
		frappe.db.set_value("Item Display Order Import Tool", docname, "status", status,
			update_modified=False)
		frappe.db.commit()
		frappe.publish_realtime("msgprint",
			f"Display order import {docname} finished: {doc.updated} updated, {doc.failed} failed, "
			f"{doc.rows_not_found or 0} skipped (item not found), "
			f"{doc.variants_pinged or 0} variant(s) pinged.",
			user=frappe.session.user)
