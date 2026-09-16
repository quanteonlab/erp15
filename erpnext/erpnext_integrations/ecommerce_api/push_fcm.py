"""FCM HTTP v1 sender for Silkos POS push notifications.

Credentials (pick one):
  - site_config.json key ``fcm_service_account`` = full service-account JSON dict
  - site_config.json key ``fcm_service_account_path`` = absolute path to JSON file
  - env ``FCM_SERVICE_ACCOUNT_JSON`` = JSON string
  - env ``FCM_SERVICE_ACCOUNT_PATH`` = path to JSON file

Optional: ``fcm_project_id`` (otherwise taken from the service-account ``project_id``).
"""

from __future__ import annotations

import json
import os

import frappe
import requests


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _load_service_account() -> dict | None:
	conf = frappe.conf or {}
	raw = conf.get("fcm_service_account")
	if isinstance(raw, dict) and raw.get("private_key"):
		return raw
	if isinstance(raw, str) and raw.strip().startswith("{"):
		try:
			return json.loads(raw)
		except Exception:
			pass

	path = conf.get("fcm_service_account_path") or os.environ.get("FCM_SERVICE_ACCOUNT_PATH")
	if path and os.path.isfile(path):
		with open(path, encoding="utf-8") as f:
			return json.load(f)

	env_json = os.environ.get("FCM_SERVICE_ACCOUNT_JSON")
	if env_json:
		try:
			return json.loads(env_json)
		except Exception:
			frappe.log_error(title="FCM_SERVICE_ACCOUNT_JSON is not valid JSON")
	return None


def fcm_configured() -> bool:
	return bool(_load_service_account())


def _access_token(sa: dict) -> str:
	try:
		from google.oauth2 import service_account
		import google.auth.transport.requests
	except ImportError:
		frappe.throw(
			"google-auth is required to send FCM pushes. "
			"Install with: bench pip install google-auth requests"
		)

	creds = service_account.Credentials.from_service_account_info(sa, scopes=[FCM_SCOPE])
	creds.refresh(google.auth.transport.requests.Request())
	return creds.token


def send_fcm_message(
	*,
	token: str,
	title: str,
	body: str,
	data: dict | None = None,
) -> dict:
	"""Send one data+notification message to a device token. Returns provider response summary."""
	sa = _load_service_account()
	if not sa:
		frappe.throw(
			"FCM is not configured. Set fcm_service_account in site_config.json "
			"(see local_docs/proposals/i033_flutter_push_notifications.md)."
		)

	project_id = (
		frappe.conf.get("fcm_project_id")
		or sa.get("project_id")
		or os.environ.get("FCM_PROJECT_ID")
	)
	if not project_id:
		frappe.throw("FCM project_id missing (service account or fcm_project_id).")

	access = _access_token(sa)
	url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

	# FCM data values must be strings
	data_str = {str(k): "" if v is None else str(v) for k, v in (data or {}).items()}

	payload = {
		"message": {
			"token": token,
			"notification": {"title": title or "", "body": body or ""},
			"data": data_str,
			"android": {"priority": "high"},
			"apns": {"headers": {"apns-priority": "10"}},
		}
	}

	resp = requests.post(
		url,
		headers={
			"Authorization": f"Bearer {access}",
			"Content-Type": "application/json; charset=UTF-8",
		},
		json=payload,
		timeout=20,
	)
	try:
		body_json = resp.json()
	except Exception:
		body_json = {"raw": (resp.text or "")[:500]}

	ok = 200 <= resp.status_code < 300
	return {
		"ok": ok,
		"status": resp.status_code,
		"response": body_json,
	}
