"""Restricted mini-Jupyter runtime for spreadsheet snippets.

Preloads numpy / pandas / matplotlib / seaborn when installed, injects
reserved accounting variables, and captures stdout + matplotlib figures.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import traceback
from contextlib import redirect_stdout

ALLOWED_IMPORT_ROOTS = {
	"numpy",
	"pandas",
	"matplotlib",
	"seaborn",
	"math",
	"statistics",
	"datetime",
	"json",
	"collections",
	"itertools",
	"functools",
	"decimal",
	"re",
}

BLOCKED_NAMES = {
	"__import__",
	"eval",
	"exec",
	"compile",
	"open",
	"input",
	"breakpoint",
	"exit",
	"quit",
	"help",
	"memoryview",
	"globals",
	"locals",
	"vars",
	"getattr",
	"setattr",
	"delattr",
	"classmethod",
	"staticmethod",
	"__class__",
	"__bases__",
	"__subclasses__",
}


class SnippetError(Exception):
	pass


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
	if level:
		raise ImportError("relative import is not allowed")
	root = (name or "").split(".")[0]
	if root not in ALLOWED_IMPORT_ROOTS:
		raise ImportError(f"import '{name}' is not allowed")
	return __import__(name, globals, locals, fromlist, level)


class Reserved(dict):
	"""dict + attribute access + search() over pre-applied variables."""

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as e:
			raise AttributeError(key) from e

	def search(self, query=""):
		q = str(query or "").strip().lower()
		if not q:
			return dict(self)
		return {
			k: v
			for k, v in self.items()
			if q in str(k).lower() or q in str(v).lower()
		}


def _load_plot_libs():
	missing = []
	mods = {}
	try:
		import numpy as np

		mods["np"] = np
		mods["numpy"] = np
	except Exception:
		missing.append("numpy")
	try:
		import pandas as pd

		mods["pd"] = pd
		mods["pandas"] = pd
	except Exception:
		missing.append("pandas")
	try:
		import matplotlib

		matplotlib.use("Agg")
		import matplotlib.pyplot as plt

		mods["plt"] = plt
		mods["matplotlib"] = matplotlib
	except Exception:
		missing.append("matplotlib")
	try:
		import seaborn as sns

		mods["sns"] = sns
		mods["seaborn"] = sns
	except Exception:
		missing.append("seaborn")
	return mods, missing


def _capture_figures(plt):
	images = []
	if plt is None:
		return images
	for num in list(plt.get_fignums()):
		fig = plt.figure(num)
		buf = io.BytesIO()
		fig.savefig(buf, format="png", bbox_inches="tight", dpi=90)
		plt.close(fig)
		b64 = base64.b64encode(buf.getvalue()).decode("ascii")
		if len(b64) > 450_000:
			continue
		images.append(b64)
	return images


def _validate_source(source: str):
	tree = ast.parse(source or "")
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.level:
			raise SnippetError("#DENIED")
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			names = []
			if isinstance(node, ast.Import):
				names = [a.name.split(".")[0] for a in node.names]
			else:
				names = [(node.module or "").split(".")[0]]
			for n in names:
				if n and n not in ALLOWED_IMPORT_ROOTS:
					raise SnippetError(f"#IMPORT {n}")
		if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
			raise SnippetError("#DENIED")
		if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
			raise SnippetError("#DENIED")


def _exec_cell(source: str, ns: dict):
	"""Run a cell like Jupyter: statements, then display the last expression."""
	tree = ast.parse(source or "")
	if not tree.body:
		return
	last = tree.body[-1]
	if isinstance(last, ast.Expr):
		head = ast.Module(body=tree.body[:-1], type_ignores=[])
		ast.fix_missing_locations(head)
		if head.body:
			exec(compile(head, "<cell>", "exec"), ns, ns)
		val = eval(compile(ast.Expression(last.value), "<cell>", "eval"), ns, ns)
		if val is None:
			return
		to_s = getattr(val, "to_string", None)
		print(to_s() if callable(to_s) else val)
		return
	exec(compile(source, "<cell>", "exec"), ns, ns)


def _namespace(reserved_map: dict, frames=None):
	mods, missing = _load_plot_libs()
	reserved = Reserved()
	for k, v in (reserved_map or {}).items():
		key = str(k)
		if key.isidentifier():
			reserved[key] = v
	safe_builtins = {
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
		"print": print,
		"range": range,
		"round": round,
		"sorted": sorted,
		"str": str,
		"sum": sum,
		"tuple": tuple,
		"zip": zip,
		"set": set,
		"frozenset": frozenset,
		"reversed": reversed,
		"isinstance": isinstance,
		"type": type,
		"Exception": Exception,
		"ValueError": ValueError,
		"TypeError": TypeError,
		"True": True,
		"False": False,
		"None": None,
		"__import__": _safe_import,
	}
	ns = {
		"__builtins__": safe_builtins,
		"reserved": reserved,
		"R": reserved,
		"data": None,
	}
	ns.update(mods)
	for k, v in reserved.items():
		if k not in ns:
			ns[k] = v
	pd = mods.get("pd")
	if pd is not None:
		try:
			ns["data"] = pd.DataFrame([dict(reserved)])
		except Exception:
			ns["data"] = reserved
	else:
		ns["data"] = reserved
	ns["_missing_libs"] = missing
	store = frames if isinstance(frames, dict) else {}

	def get_df(key):
		k = str(key or "").strip()
		rows = store.get(k)
		if rows is None:
			raise KeyError(f"unknown table {k!r} — use get_df('t01') or the table name")
		pd = mods.get("pd")
		if pd is not None:
			return pd.DataFrame(rows)
		return rows

	ns["get_df"] = get_df
	ns["get_df"] = get_df
	return ns


def run_cells(cells: list, reserved_map: dict, frames=None) -> dict:
	"""Execute notebook cells in order. Returns {output_id: payload}."""
	MAX_CELLS = 40
	MAX_SOURCE = 20_000
	ns = _namespace(reserved_map, frames)
	plt = ns.get("plt")
	out = {}
	for cell in (cells or [])[:MAX_CELLS]:
		oid = str(cell.get("output_id") or "").strip() or "out_00"
		source = str(cell.get("source") or "")[:MAX_SOURCE]
		payload = {
			"output_id": oid,
			"type": "empty",
			"text": "",
			"image": "",
			"error": "",
		}
		if not source.strip():
			out[oid] = payload
			continue
		try:
			_validate_source(source)
		except SnippetError as e:
			payload["type"] = "error"
			payload["error"] = str(e)
			out[oid] = payload
			continue
		except SyntaxError as e:
			payload["type"] = "error"
			payload["error"] = f"SyntaxError: {e}"
			out[oid] = payload
			continue
		buf = io.StringIO()
		try:
			with redirect_stdout(buf):
				_exec_cell(source, ns)
			text = buf.getvalue().strip()[:8000]
			images = _capture_figures(plt)
			if images:
				payload["type"] = "image"
				payload["image"] = images[-1]
				payload["text"] = text
			elif text:
				payload["type"] = "text"
				payload["text"] = text
			else:
				missing = ns.get("_missing_libs") or []
				src_l = source.lower()
				needs = [m for m in missing if m in src_l or (m == "matplotlib" and "plt" in src_l) or (m == "numpy" and "np." in src_l) or (m == "pandas" and "pd." in src_l) or (m == "seaborn" and "sns" in src_l)]
				if needs:
					payload["type"] = "error"
					payload["error"] = "Missing libraries: " + ", ".join(needs)
				else:
					payload["type"] = "empty"
		except Exception:
			payload["type"] = "error"
			payload["error"] = traceback.format_exc(limit=8)[:4000]
			payload["text"] = buf.getvalue().strip()[:8000]
		out[oid] = payload
	return out


def parse_namespace(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			raw = {}
	if not isinstance(raw, dict):
		return {}
	clean = {}
	for k, v in raw.items():
		key = str(k)
		if isinstance(v, (int, float)) and not isinstance(v, bool):
			clean[key] = float(v)
		elif isinstance(v, str) and len(v) < 400:
			clean[key] = v
	return clean


run_cells = run_cells
