# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, getdate


class StockLot(Document):
	def validate(self):
		self._sync_sell_by_date()
		self._sync_status()

	def _sync_sell_by_date(self):
		days = cint(self.sell_by_days or 0)
		if days > 0 and self.receive_date:
			self.sell_by_date = add_days(getdate(self.receive_date), days)
		elif not days:
			self.sell_by_date = None

	def _sync_status(self):
		remaining = flt(self.qty_remaining)
		if remaining <= 0.0001:
			self.qty_remaining = 0
			self.status = "Depleted"
			return
		# Soft expiry estimate — does not block stock, only labels the lot.
		if self.sell_by_date and getdate(self.sell_by_date) < getdate():
			self.status = "Expired Est."
		elif self.status == "Depleted":
			self.status = "Open"
