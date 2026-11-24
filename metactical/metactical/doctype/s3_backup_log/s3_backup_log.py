# Copyright (c) 2026, Metactical and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class S3BackupLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		log_datetime: DF.Datetime
		site: DF.Data | None
		status: DF.Literal["Started", "Success", "Failed"]
		steps: DF.JSON | None
		summary: DF.SmallText | None

	# end: auto-generated types
	pass
