"""Employee + Employee Group APIs for Tables > Empleados."""

from __future__ import annotations

import secrets
import string

import frappe
from frappe import _
from frappe.utils import cint, flt, today

from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

# Roles commonly useful for store staff (filter assignable list)
PREFERRED_ROLES = [
	"Employee",
	"Sales User",
	"Stock User",
	"Accounts User",
	"Purchase User",
	"HR User",
	"Manufacturing User",
	"Item Manager",
]


def _rand_password(length: int = 12) -> str:
	alphabet = string.ascii_letters + string.digits
	# Ensure mixed classes
	chars = [
		secrets.choice(string.ascii_uppercase),
		secrets.choice(string.ascii_lowercase),
		secrets.choice(string.digits),
	]
	chars += [secrets.choice(alphabet) for _ in range(max(0, length - 3))]
	secrets.SystemRandom().shuffle(chars)
	return "".join(chars)


def _serialize_employee(name: str) -> dict:
	emp = frappe.get_doc("Employee", name)
	groups = frappe.db.sql(
		"""
		SELECT parent AS group_name
		FROM `tabEmployee Group Table`
		WHERE employee = %s
		ORDER BY parent
		""",
		name,
		as_dict=True,
	)
	roles: list[str] = []
	user_enabled = None
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		roles = frappe.get_roles(emp.user_id)
		roles = [r for r in roles if r not in ("All", "Guest", "Desk User")]
		user_enabled = cint(frappe.db.get_value("User", emp.user_id, "enabled"))
	return {
		"name": emp.name,
		"employee_name": emp.employee_name,
		"first_name": emp.first_name,
		"last_name": emp.last_name,
		"status": emp.status,
		"company": emp.company,
		"branch": emp.branch,
		"department": emp.department,
		"designation": emp.designation,
		"cell_number": emp.cell_number,
		"company_email": emp.company_email,
		"personal_email": emp.personal_email,
		"prefered_email": emp.prefered_email,
		"ctc": emp.ctc,
		"salary_currency": emp.salary_currency,
		"bio": emp.bio,
		"user_id": emp.user_id,
		"has_user": bool(emp.user_id),
		"user_enabled": user_enabled,
		"roles": roles,
		"groups": [g.group_name for g in groups],
		"date_of_joining": str(emp.date_of_joining) if emp.date_of_joining else None,
		"modified": str(emp.modified) if emp.modified else None,
	}


@frappe.whitelist()
def list_employees(search=None, status=None, page=1, page_length=100):
	page = max(1, cint(page) or 1)
	page_length = max(1, min(500, cint(page_length) or 100))
	filters = {}
	if status:
		filters["status"] = status
	or_filters = None
	if search and str(search).strip():
		q = f"%{str(search).strip()}%"
		or_filters = [
			["employee_name", "like", q],
			["name", "like", q],
			["user_id", "like", q],
			["branch", "like", q],
			["cell_number", "like", q],
		]
	names = frappe.get_all(
		"Employee",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="employee_name asc",
		limit_start=(page - 1) * page_length,
		limit_page_length=page_length,
	)
	total = frappe.db.count("Employee", filters=filters)
	return {"rows": [_serialize_employee(n) for n in names], "total": total}


@frappe.whitelist()
def get_employee(name):
	if not frappe.db.exists("Employee", name):
		frappe.throw(_("Employee {0} not found").format(name))
	return _serialize_employee(name)


@frappe.whitelist()
def save_employee(name=None, data=None):
	"""Create or update Employee. data: JSON/dict of fields."""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = data or {}
	is_new = not name
	tracked = [
		"employee_name",
		"first_name",
		"last_name",
		"status",
		"branch",
		"department",
		"designation",
		"cell_number",
		"company_email",
		"personal_email",
		"prefered_email",
		"ctc",
		"bio",
		"date_of_joining",
	]

	if is_new:
		company = data.get("company") or frappe.defaults.get_user_default("Company")
		if not company:
			company = frappe.db.get_value("Company", {}, "name")
		first = (data.get("first_name") or "").strip()
		last = (data.get("last_name") or "").strip()
		full = (data.get("employee_name") or "").strip() or f"{first} {last}".strip()
		if not first and full:
			parts = full.split()
			first = parts[0]
			last = " ".join(parts[1:]) if len(parts) > 1 else ""
		if not first:
			frappe.throw(_("First name / employee name is required"))
		doc = frappe.new_doc("Employee")
		doc.company = company
		doc.first_name = first
		doc.last_name = last or None
		doc.employee_name = full or first
		doc.status = data.get("status") or "Active"
		doc.date_of_joining = data.get("date_of_joining") or today()
		doc.gender = data.get("gender") or "Prefer not to say"
		doc.date_of_birth = data.get("date_of_birth") or "1990-01-01"
		for key in tracked:
			if key in data and data[key] is not None and key not in (
				"first_name",
				"last_name",
				"employee_name",
				"status",
				"date_of_joining",
			):
				doc.set(key, data[key])
		if "ctc" in data:
			doc.ctc = flt(data.get("ctc"))
		doc.insert()
		log_field_changes(
			"Employee",
			doc.name,
			[("employee_name", None, doc.employee_name), ("status", None, doc.status)],
		)
	else:
		if not frappe.db.exists("Employee", name):
			frappe.throw(_("Employee {0} not found").format(name))
		doc = frappe.get_doc("Employee", name)
		changes = []
		for key in tracked:
			if key not in data:
				continue
			new_val = data[key]
			if key == "ctc":
				new_val = flt(new_val) if new_val not in (None, "") else None
			old_val = doc.get(key)
			if str(old_val or "") != str(new_val or ""):
				changes.append((key, old_val, new_val))
				doc.set(key, new_val)
		# Keep employee_name in sync
		if data.get("first_name") or data.get("last_name") or data.get("employee_name"):
			first = (data.get("first_name") or doc.first_name or "").strip()
			last = (data.get("last_name") or doc.last_name or "").strip()
			full = (data.get("employee_name") or "").strip() or f"{first} {last}".strip()
			if full and full != doc.employee_name:
				changes.append(("employee_name", doc.employee_name, full))
				doc.employee_name = full
			if first:
				doc.first_name = first
			if last is not None:
				doc.last_name = last or None
		doc.save()
		if changes:
			log_field_changes("Employee", doc.name, changes)

	# Groups membership
	if "groups" in data:
		_set_employee_groups(doc.name, data.get("groups") or [])

	# Roles on linked user
	if "roles" in data and doc.user_id:
		_set_user_roles(doc.user_id, data.get("roles") or [])

	frappe.db.commit()
	return {"ok": True, "employee": _serialize_employee(doc.name)}


def _set_employee_groups(employee: str, group_names: list[str]) -> None:
	wanted = set(g for g in group_names if g)
	current = {
		r.parent
		for r in frappe.get_all(
			"Employee Group Table",
			filters={"employee": employee},
			fields=["parent", "name"],
		)
	}
	# Remove from groups not wanted
	for g in current - wanted:
		rows = frappe.get_all(
			"Employee Group Table",
			filters={"parent": g, "employee": employee},
			pluck="name",
		)
		for row_name in rows:
			frappe.delete_doc("Employee Group Table", row_name, ignore_permissions=True)
	# Add to missing groups
	emp_name = frappe.db.get_value("Employee", employee, "employee_name")
	user_id = frappe.db.get_value("Employee", employee, "user_id")
	for g in wanted - current:
		if not frappe.db.exists("Employee Group", g):
			continue
		parent = frappe.get_doc("Employee Group", g)
		parent.append(
			"employee_list",
			{"employee": employee, "employee_name": emp_name, "user_id": user_id},
		)
		parent.save(ignore_permissions=True)


def _set_user_roles(user: str, roles: list[str]) -> None:
	user_doc = frappe.get_doc("User", user)
	# Keep system roles intact; replace assignable set
	protected = {"All", "Guest", "Administrator"}
	current = {d.role for d in user_doc.roles}
	desired = set(roles) | (current & protected)
	# Always keep Desk User if they have any desk role
	if desired - {"All", "Guest"}:
		desired.add("Desk User")
	user_doc.set("roles", [])
	for r in sorted(desired):
		if frappe.db.exists("Role", r):
			user_doc.append("roles", {"role": r})
	user_doc.save(ignore_permissions=True)


@frappe.whitelist()
def create_employee_user(employee, email=None, roles=None):
	"""Create User for employee with a random password (returned once)."""
	if isinstance(roles, str):
		roles = frappe.parse_json(roles)
	roles = roles or ["Employee"]
	emp = frappe.get_doc("Employee", employee)
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Employee already has user {0}").format(emp.user_id))

	email = (email or emp.prefered_email or emp.company_email or emp.personal_email or "").strip()
	if not email:
		# Synthetic email so User can be created
		slug = "".join(ch for ch in (emp.employee_name or emp.name).lower() if ch.isalnum())[:20]
		email = f"{slug or emp.name.lower()}@employees.local"
	emp.prefered_email = email
	emp.company_email = emp.company_email or email
	emp.save(ignore_permissions=True)

	password = _rand_password()
	parts = (emp.employee_name or emp.first_name or "User").split()
	first = parts[0]
	last = " ".join(parts[1:]) if len(parts) > 1 else ""

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first,
			"last_name": last or None,
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"new_password": password,
		}
	)
	user.insert(ignore_permissions=True)
	_set_user_roles(user.name, list(roles) if "Employee" in roles else list(roles) + ["Employee"])

	emp.user_id = user.name
	emp.create_user_permission = 1
	emp.save(ignore_permissions=True)
	log_field_changes("Employee", emp.name, [("user_id", None, user.name)])
	frappe.db.commit()
	return {
		"ok": True,
		"user_id": user.name,
		"email": email,
		"password": password,
		"employee": _serialize_employee(emp.name),
	}


@frappe.whitelist()
def reset_employee_user_password(employee):
	"""Generate a new random password for the linked user (returned once)."""
	emp = frappe.get_doc("Employee", employee)
	if not emp.user_id or not frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Employee has no user"))
	password = _rand_password()
	user = frappe.get_doc("User", emp.user_id)
	user.new_password = password
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "user_id": user.name, "email": user.email, "password": password}


@frappe.whitelist()
def list_employee_groups():
	names = frappe.get_all("Employee Group", pluck="name", order_by="name asc")
	out = []
	for n in names:
		doc = frappe.get_doc("Employee Group", n)
		out.append(
			{
				"name": doc.name,
				"employee_group_name": doc.employee_group_name,
				"members": [
					{
						"employee": r.employee,
						"employee_name": r.employee_name,
						"user_id": r.user_id,
					}
					for r in (doc.employee_list or [])
				],
			}
		)
	return {"groups": out}


@frappe.whitelist()
def save_employee_group(name=None, employee_group_name=None, members=None):
	if isinstance(members, str):
		members = frappe.parse_json(members)
	members = members or []
	title = (employee_group_name or name or "").strip()
	if not title:
		frappe.throw(_("Group name is required"))

	if name and frappe.db.exists("Employee Group", name):
		doc = frappe.get_doc("Employee Group", name)
		if title != doc.employee_group_name:
			# Employee Group name is typically the title
			pass
		doc.set("employee_list", [])
	else:
		if frappe.db.exists("Employee Group", title):
			frappe.throw(_("Group {0} already exists").format(title))
		doc = frappe.new_doc("Employee Group")
		doc.employee_group_name = title

	for m in members:
		emp = m.get("employee") if isinstance(m, dict) else m
		if not emp or not frappe.db.exists("Employee", emp):
			continue
		emp_name, user_id = frappe.db.get_value("Employee", emp, ["employee_name", "user_id"])
		doc.append(
			"employee_list",
			{"employee": emp, "employee_name": emp_name, "user_id": user_id},
		)
	if name and frappe.db.exists("Employee Group", name):
		doc.save()
	else:
		doc.insert()
	frappe.db.commit()
	return {"ok": True, "group": {"name": doc.name, "employee_group_name": doc.employee_group_name}}


@frappe.whitelist()
def delete_employee_group(name):
	if not frappe.db.exists("Employee Group", name):
		frappe.throw(_("Group {0} not found").format(name))
	frappe.delete_doc("Employee Group", name)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def list_employee_meta():
	"""Branches, companies, assignable roles for the editor UI."""
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	branches = frappe.get_all("Branch", pluck="name", order_by="name asc") if frappe.db.exists("DocType", "Branch") else []
	roles = frappe.get_all(
		"Role",
		filters={"disabled": 0},
		pluck="name",
		order_by="name asc",
	)
	# Prefer common roles first
	pref = [r for r in PREFERRED_ROLES if r in roles]
	rest = [r for r in roles if r not in pref and r not in ("Administrator", "Guest", "All")]
	return {
		"company": company,
		"branches": branches or [],
		"roles": pref + rest[:40],
	}
