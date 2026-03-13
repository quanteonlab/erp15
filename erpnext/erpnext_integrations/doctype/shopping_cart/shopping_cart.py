# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ShoppingCart(Document):
	def before_save(self):
		from frappe.utils import now_datetime
		if not self.created_at:
			self.created_at = now_datetime()
		self.updated_at = now_datetime()
