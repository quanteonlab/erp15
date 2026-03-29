# Copyright (c) 2025, Administrator and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class FlightPassenger(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		date_of_birth: DF.Date
		first_name: DF.Data
		full_name: DF.Data | None
		last_name: DF.Data | None
	# end: auto-generated types

	def validate(self):
		first = (self.first_name or "").strip()
		last = (self.last_name or "").strip()
		self.full_name = " ".join([p for p in [first, last] if p])