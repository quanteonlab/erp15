# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class ItemWeightObservation(Document):
	def validate(self):
		units = flt(self.units)
		total = flt(self.total_weight)
		if units > 0:
			self.kg_per_unit = total / units
		elif self.kg_per_unit in (None, ""):
			self.kg_per_unit = None
