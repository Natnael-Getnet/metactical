// Copyright (c) 2026, Techlift Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Item Display Order Import Tool", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		const done = (frm.doc.updated || 0) + (frm.doc.failed || 0);
		const total = frm.doc.rows_to_update || 0;
		const running = ["Queued", "In Progress"].includes(frm.doc.status);

		frm.dashboard.set_headline(
			__("{0} of {1} processed: {2} updated, {3} failed, {4} skipped (item not found).",
				[done, total, frm.doc.updated || 0, frm.doc.failed || 0, frm.doc.rows_not_found || 0]),
			running ? "orange" : (frm.doc.failed ? "red" : "green")
		);

		if (running) {
			// keep the counts moving while the background jobs run
			clearTimeout(frm.__ido_refresh);
			frm.__ido_refresh = setTimeout(() => frm.reload_doc(), 10000);
		}
	},
});
