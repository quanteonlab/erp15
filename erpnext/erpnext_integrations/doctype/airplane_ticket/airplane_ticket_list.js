frappe.listview_settings['Airplane Ticket'] = {
  add_fields: ['status'],
  get_indicator(doc) {
    const colors = { 'Booked': 'gray', 'Checked-In': 'purple', 'Boarded': 'green' };
    return [__(doc.status), colors[doc.status] || 'blue', `status,=,${doc.status}`];
  },
};



