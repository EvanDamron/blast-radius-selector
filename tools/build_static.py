#!/usr/bin/env python3
"""Static facts about pinned sqlglot, written to map/static.json.gz.

Run with the venv Python from the sqlglot checkout on sys.path:
    work/venv/bin/python tools/build_static.py work/sqlglot

{"defs": {name: [[relpath, body_first, last], ...]},       # every function by name
 "expr": {"module:Qualname": [relpath, [descendant keys incl. itself]]},
 "expr_by_name": {ClassName: [keys]}, "expr_by_key": {sql key: [keys]}}
"""
import ast
import gzip
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(src))

defs: dict = {}
for sub in ("sqlglot", "tests"):
    for p in sorted((src / sub).rglob("*.py")):
        rel = str(p.relative_to(src))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append([rel, node.body[0].lineno, node.end_lineno])

import sqlglot  # noqa: E402
from sqlglot.dialects import DIALECTS  # noqa: E402
from sqlglot.expressions.core import Expr  # noqa: E402

for name in DIALECTS:
    importlib.import_module(f"sqlglot.dialects.{name.lower()}")


def key(c):
    return f"{c.__module__}:{c.__qualname__}"


def all_subclasses(c):
    out, stack = set(), [c]
    while stack:
        k = stack.pop()
        for s in k.__subclasses__():
            if s not in out:
                out.add(s)
                stack.append(s)
    return out


classes = all_subclasses(Expr) | {Expr}
expr = {}
by_name: dict = {}
by_key: dict = {}
for c in classes:
    mod = sys.modules.get(c.__module__)
    f = getattr(mod, "__file__", None) or ""
    rel = str(Path(f).resolve().relative_to(src)) if f.startswith(str(src)) else ""
    expr[key(c)] = [rel, sorted(key(d) for d in all_subclasses(c) | {c})]
    by_name.setdefault(c.__name__, []).append(key(c))
    by_key.setdefault(getattr(c, "key", c.__name__.lower()), []).append(key(c))

out = {"defs": defs, "expr": expr, "expr_by_name": by_name, "expr_by_key": by_key}
blob = json.dumps(out, separators=(",", ":")).encode()
with gzip.open(ROOT / "map" / "static.json.gz", "wb", compresslevel=9) as fh:
    fh.write(blob)
print(f"defs={len(defs)} expr={len(expr)} raw={len(blob) / 1e6:.1f}MB")
