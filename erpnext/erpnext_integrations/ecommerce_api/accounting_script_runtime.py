"""RestrictedPython sandbox for the Accounting mini-notebook (v1).

No frappe, no imports, no network — only injected constants + evaluated grids
and a ``chart()`` helper that returns JSON chart specs for the frontend.
"""

from __future__ import annotations

import json
import signal
from contextlib import contextmanager

from RestrictedPython import PrintCollector, compile_restricted
from RestrictedPython.Guards import guarded_iter_unpack_sequence, safe_builtins

MAX_CODE_LEN = 16_384
EXEC_TIMEOUT_SEC = 2
ALLOWED_CHART_KINDS = frozenset({"bar", "line", "pie"})


class ScriptTimeout(Exception):
	pass


class ScriptError(Exception):
	pass


def _timeout_handler(_signum, _frame):
	raise ScriptTimeout("Script timed out after 2s")


@contextmanager
def _exec_timeout(seconds: int):
	if seconds <= 0:
		yield
		return
	prev = signal.signal(signal.SIGALRM, _timeout_handler)
	signal.alarm(seconds)
	try:
		yield
	finally:
		signal.alarm(0)
		signal.signal(signal.SIGALRM, prev)


def _coerce_constants(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			raw = {}
	if not isinstance(raw, dict):
		return {}
	out = {}
	for k, v in raw.items():
		key = str(k)
		if isinstance(v, bool):
			continue
		if isinstance(v, (int, float)):
			out[key] = float(v)
		elif isinstance(v, str) and len(v) < 400:
			out[key] = v
	return out


def _coerce_grid(raw) -> list:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			return []
	if not isinstance(raw, list):
		return []
	rows = []
	for row in raw[:120]:
		if not isinstance(row, list):
			continue
		cells = []
		for cell in row[:40]:
			if cell is None or cell == "":
				cells.append(None)
			elif isinstance(cell, bool):
				cells.append(None)
			elif isinstance(cell, (int, float)):
				cells.append(float(cell))
			else:
				cells.append(str(cell)[:200])
		rows.append(cells)
	return rows


def _coerce_sheets(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			return {}
	if not isinstance(raw, dict):
		return {}
	out = {}
	for name, grid in raw.items():
		out[str(name)] = _coerce_grid(grid)
	return out


def _validate_chart(kind: str, labels, values, title=""):
	k = str(kind or "bar").strip().lower()
	if k not in ALLOWED_CHART_KINDS:
		raise ScriptError(f"chart kind must be one of {sorted(ALLOWED_CHART_KINDS)}")
	if not isinstance(labels, (list, tuple)) or not isinstance(values, (list, tuple)):
		raise ScriptError("labels and values must be lists")
	if len(labels) != len(values):
		raise ScriptError("labels and values must have the same length")
	if len(labels) > 40:
		raise ScriptError("too many chart points (max 40)")
	nums = []
	for v in values:
		if isinstance(v, bool) or not isinstance(v, (int, float)):
			raise ScriptError("chart values must be numbers")
		nums.append(float(v))
	str_labels = [str(x)[:80] for x in labels]
	return {
		"kind": k,
		"title": str(title or "")[:120],
		"labels": str_labels,
		"values": nums,
	}


def _guarded_getitem(obj, key):
	if isinstance(key, str) and key.startswith("_"):
		raise KeyError(key)
	return obj[key]


def run_accounting_script(code: str, constants=None, grid=None, sheets=None) -> dict:
	"""Execute user script; return {ok, stdout, charts, error}."""
	source = str(code or "")
	if len(source) > MAX_CODE_LEN:
		return {"ok": False, "stdout": "", "charts": [], "error": "Script too long (max 16KB)"}

	constants_map = _coerce_constants(constants)
	grid_rows = _coerce_grid(grid)
	sheets_map = _coerce_sheets(sheets)
	charts: list = []

	def chart(kind, labels, values, title=""):
		spec = _validate_chart(kind, labels, values, title)
		charts.append(spec)
		return spec

	safe = safe_builtins.copy()
	safe.update(
		{
			"abs": abs,
			"all": all,
			"any": any,
			"bool": bool,
			"dict": dict,
			"enumerate": enumerate,
			"filter": filter,
			"float": float,
			"int": int,
			"len": len,
			"list": list,
			"map": map,
			"max": max,
			"min": min,
			"range": range,
			"round": round,
			"sorted": sorted,
			"str": str,
			"sum": sum,
			"tuple": tuple,
			"zip": zip,
			"set": set,
			"isinstance": isinstance,
			"True": True,
			"False": False,
			"None": None,
		}
	)

	exec_globals = {
		"__builtins__": safe,
		"_getattr_": getattr,
		"_getitem_": _guarded_getitem,
		"_getiter_": iter,
		"_iter_unpack_sequence_": guarded_iter_unpack_sequence,
		"_print_": PrintCollector,
		"_write_": lambda x: x,
		"constants": constants_map,
		"grid": grid_rows,
		"sheets": sheets_map,
		"chart": chart,
	}
	exec_locals = {}

	if not source.strip():
		return {"ok": True, "stdout": "", "charts": [], "error": ""}

	try:
		compiled = compile_restricted(source, "<accounting_script>", "exec")
		errors = getattr(compiled, "errors", None)
		if errors:
			return {"ok": False, "stdout": "", "charts": [], "error": "; ".join(errors)}
		code_obj = getattr(compiled, "code", compiled)
		with _exec_timeout(EXEC_TIMEOUT_SEC):
			exec(code_obj, exec_globals, exec_locals)
		stdout = ""
		print_fn = exec_locals.get("_print")
		if callable(print_fn):
			stdout = str(print_fn()).strip()[:8000]
		return {"ok": True, "stdout": stdout, "charts": charts, "error": ""}
	except ScriptTimeout as e:
		return {"ok": False, "stdout": "", "charts": [], "error": str(e)}
	except SyntaxError as e:
		return {"ok": False, "stdout": "", "charts": [], "error": f"SyntaxError: {e}"}
	except Exception as e:
		msg = str(e) or e.__class__.__name__
		if "Import" in msg or "import" in msg.lower():
			msg = f"Imports are not allowed: {msg}"
		return {"ok": False, "stdout": "", "charts": [], "error": msg[:4000]}
