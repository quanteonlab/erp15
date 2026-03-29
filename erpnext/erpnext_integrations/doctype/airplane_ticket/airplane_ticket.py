# Copyright (c) 2025, Administrator and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AirplaneTicket(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from erpnext.erpnext_integrations.doctype.airplane_ticket_add_on_item.airplane_ticket_add_on_item import AirplaneTicketAddOnItem
		from frappe.types import DF

		add_ons: DF.Table[AirplaneTicketAddOnItem]
		departure_date: DF.Date
		departure_time: DF.Time
		destination_airport: DF.Link
		destination_airport_code: DF.Data | None
		duration: DF.Duration
		flight: DF.Link
		passenger: DF.Link
		source_airport: DF.Link
		source_airport_code: DF.Data | None
		status: DF.Literal["Booked", "Checked-In", "Boarded"]
	# end: auto-generated types

	def validate(self):
		if self.source_airport and self.destination_airport and self.source_airport == self.destination_airport:
			frappe.throw("Source and Destination Airport must be different.")
		# Optional: compute total of add-ons if you add a total field later
		# self.addons_total = sum([(row.amount or 0) for row in (self.add_ons or [])])