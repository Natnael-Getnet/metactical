# Copyright (c) 2025, Techlift Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _, msgprint
from frappe.model.document import Document
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file, read_xls_file_from_attached_file
from frappe.model.docstatus import DocStatus
from frappe.utils import flt
from frappe.utils.file_manager import save_file
from metactical.custom_scripts.utils.metactical_utils import queue_action
import os


class ItemSupplierImportTool(Document):
	def save(self):
		if self.docstatus == DocStatus.submitted() and \
			self.ais_queue_status and self.ais_queue_status != "Queued":
			msgprint(
				_(
					"The task has been enqueued as a background job. In case there is \
					any issue on processing in background, the system will add a comment \
					about the error on this document and revert to the Draft stage"
				)
			)
			queue_action(self, "submit", timeout=2000)
		else:
			super().save()

	def on_submit(self):
		file_content = self.check_file()
		self.edit_item_supplier(file_content)

	def read_file(self):
		file_path = self.excel_file
		extn = os.path.splitext(file_path)[1][1:]

		file_content = None

		file_name = frappe.db.get_value("File", {"file_url": file_path})
		if file_name:
			file = frappe.get_doc("File", file_name)
			file_content = file.get_content()

		return file_content, extn

	def validate(self):
		file_content = self.check_file()
		self.check_headers(file_content)

	def check_file(self):
		file_content, extn = self.read_file()
		if extn == "xlsx":
			file_content = read_xlsx_file_from_attached_file(fcontent=file_content)
		elif extn == "xls":
			file_content = read_xls_file_from_attached_file(file_content)
		else:
			frappe.throw("Only xls and xlsx files are supported.")
		return file_content
	
	def check_headers(self, file_content):
		get_column_map(file_content)

	def edit_item_supplier(self, data):
		columns = get_column_map(data)
		rows = data[1:]
		limit = 500
		start = 0
		# Problems with the file (a bad quantity, an item or supplier row that doesn't exist) are
		# collected over the whole file and logged once at the end: one Error Log per row buried
		# the real errors under hundreds of these. Each list holds "Row N: ..." lines.
		self._invalid_qty = []
		self._items_not_found = []
		self._suppliers_not_found = []
		self._failed = 0
		while start < len(rows):
			end = start + limit
			# +2: data[0] is the header and Excel rows start at 1
			self._edit_item_supplier(rows[start:end], columns, first_row_no=start + 2)
			start = end
		self.log_file_problems()

	def log_file_problems(self):
		sections = [
			(self._invalid_qty, "row(s) have an invalid Quantity To Update", "invalid qty"),
			(self._items_not_found, "item(s) do not exist", "item(s) not found"),
			(self._suppliers_not_found, "supplier row(s) do not exist on their item", "supplier row(s) not found"),
		]
		lines, parts = [], []
		for entries, heading, short in sections:
			if not entries:
				continue
			if lines:
				lines.append("")
			lines.append(f"{len(entries)} {heading}:")
			lines.extend(entries)
			parts.append(f"{len(entries)} {short}")

		if lines:
			title = f"Item Supplier Import {self.name}: {', '.join(parts)}"
			frappe.log_error(title=title, message="\n".join(lines),
				reference_doctype=self.doctype, reference_name=self.name)

		# One popup for the whole import instead of one per failed row
		if parts or self._failed:
			if self._failed:
				parts.append(f"{self._failed} row(s) failed with an error")
			frappe.publish_realtime("msgprint",
				f"Item Supplier Import {self.name} finished with: {', '.join(parts)}. "
				"See the Error Log for the details.",
				user=frappe.session.user)

	def _edit_item_supplier(self, data, columns, first_row_no=2):
		for row_no, row in enumerate(data, start=first_row_no):
			# A failure is undone back to here and logged, and the import moves on to the next
			# row. Without the savepoint a failed save's half-done writes stayed in the open
			# transaction and went out with the next row's commit.
			frappe.db.savepoint("item_supplier_row")
			try:
				self._edit_row(row, columns, row_no)
			except Exception:
				frappe.db.rollback(save_point="item_supplier_row")
				self._failed += 1
				item_code = get_cell(row, columns, "Item Code")
				frappe.log_error(
					title=f"Item Supplier Import {self.name}: row {row_no} ({item_code}) failed",
					reference_doctype=self.doctype, reference_name=self.name)
			finally:
				frappe.local.message_log = []

	def _edit_row(self, row, columns, row_no):
		name = get_cell(row, columns, "Item Supplier Table Name")
		raw_qty = get_cell(row, columns, "Quantity To Update")
		item_code = get_cell(row, columns, "Item Code")

		try:
			updated_qty = float(str(raw_qty).replace('+', '').strip())
		except (TypeError, ValueError):
			self._invalid_qty.append(f"Row {row_no}: {raw_qty!r} for supplier row {name} (item {item_code})")
			return
		if updated_qty > 50:
			updated_qty = 50

		if not item_code or not frappe.db.exists("Item", {"name": item_code}):
			self._items_not_found.append(f"Row {row_no}: {item_code or '(blank Item Code)'}")
			return

		item = frappe.get_doc("Item", item_code)
		supplier = next((s for s in item.supplier_items if s.name == name), None)
		if not supplier:
			self._suppliers_not_found.append(f"Row {row_no}: {name} (item {item_code})")
			return

		current = flt(supplier.ifw_supplier_qoh)
		if current == updated_qty or (current > 49 and updated_qty == 50):
			return
		supplier.ifw_supplier_qoh = updated_qty
		supplier.ifw_sqohtimestamp = format_sqoh_date(frappe.utils.getdate())
		item.save()
		frappe.db.commit()


@frappe.whitelist(methods=["POST"])
def import_item_supplier():
	uploaded_file = frappe.request.files.get('file')
	if not uploaded_file:
		frappe.throw("No file received")

	# Save file to Frappe
	file_doc = save_file(
		fname=uploaded_file.filename,
		content=uploaded_file.read(),
		dt=None,
		dn=None,
		is_private=True
	)

	item_supplier_import_tool = frappe.get_doc({
		"doctype": "Item Supplier Import Tool",
		"excel_file": file_doc.file_url
	})

	file_content = item_supplier_import_tool.check_file()
	item_supplier_import_tool.check_headers(file_content)
	missing_items_list = validate_item_supplier(file_content)

	# Insert document
	item_supplier_import_tool.insert()

	# Link the file to the doc
	file_doc.reload()
	file_doc.dt = "Item Supplier Import Tool"
	file_doc.dn = item_supplier_import_tool.name
	file_doc.save()

	# Commit before queueing so the background job can load the document
	frappe.db.commit()

	# Updating each Item is too slow to finish within the request timeout,
	# so submit in the background
	item_supplier_import_tool.reload()
	queue_action(item_supplier_import_tool, "submit", timeout=2000, enqueue_after_commit=True)
	frappe.db.commit()

	return {
		"status": "success",
		"queue_status": "Queued",
		"file_url": file_doc.file_url,
		"docname": item_supplier_import_tool.name,
		"missing_items": missing_items_list
	}


def format_sqoh_date(date):
	"""Format as e.g. 1-Jan-0001 (day without leading zero, 4-digit year)."""
	return f"{date.day}-{date.strftime('%b')}-{date.year:04d}"


REQUIRED_HEADERS = ["Item Supplier Table Name", "Supplier Part Number", "Quantity To Update", "Item Code"]


def get_column_map(data):
	"""Map header names to column indexes. Unknown columns are ignored."""
	if not data:
		frappe.throw("The Excel File is empty.")

	columns = {}
	for idx, header in enumerate(data[0]):
		header = str(header).strip() if header is not None else ""
		if header and header not in columns:
			columns[header] = idx

	missing = [h for h in REQUIRED_HEADERS if h not in columns]
	if missing:
		frappe.throw(f"Missing required header(s) in this Excel File: {', '.join(missing)}")

	return columns


def get_cell(row, columns, header):
	idx = columns[header]
	return row[idx] if idx < len(row) else None


def validate_item_supplier(data):
	columns = get_column_map(data)
	rows = data[1:]
	limit = 500
	start = 0
	all_missing = []

	while start < len(rows):
		end = start + limit
		batch_missing = _validate_item_supplier(rows[start:end], columns)
		all_missing.extend(batch_missing)
		start = end

	return all_missing


def _validate_item_supplier(data, columns):
	missing_list = []

	rows = [
		(get_cell(row, columns, "Item Supplier Table Name"), get_cell(row, columns, "Item Code"))
		for row in data
	]
	names = list({r[0] for r in rows if r[0]})
	item_codes = list({r[1] for r in rows if r[1]})

	# Look up the whole batch in two queries instead of loading every Item
	existing_items = set(frappe.get_all(
		"Item", filters={"name": ["in", item_codes]}, pluck="name"
	)) if item_codes else set()
	existing_suppliers = {
		(d.name, d.parent) for d in frappe.get_all(
			"Item Supplier",
			filters={"name": ["in", names], "parenttype": "Item", "parentfield": "supplier_items"},
			fields=["name", "parent"],
		)
	} if names else set()

	for name, item_code in rows:
		# default values for missing
		item_not_found = None
		supplier_not_found = None

		if item_code not in existing_items:
			item_not_found = item_code
		elif (name, item_code) not in existing_suppliers:
			supplier_not_found = name

		# Only append if any is missing
		if item_not_found or supplier_not_found:
			missing_list.append({
				"item not found": item_not_found,
				"item supplier not found": supplier_not_found
			})

	return missing_list