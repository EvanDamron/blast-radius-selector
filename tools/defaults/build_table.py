#!/usr/bin/env python3
"""Literal parameter defaults of every function in pinned sqlglot.

usage: build_table.py SQLGLOT_DIR OUT.json
Writes {relpath: {qualname: [[param, value], ...]}} for parameters whose default is
a literal (str, int, float, bool or None). A qualname defined twice in one file is
left out: the recorder could not tell the two apart.
"""
import ast
import json
import sys
from pathlib import Path

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def literal(node):
    """(True, value) for a literal default, else (False, None)."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return True, node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)):
        return True, -node.operand.value
    return False, None


def defaults(fn):
    a = fn.args
    pos = a.posonlyargs + a.args
    out = []
    for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
        ok, v = literal(d)
        if ok:
            out.append([arg.arg, v])
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            ok, v = literal(d)
            if ok:
                out.append([arg.arg, v])
    return out


def walk(body, qual, out, seen):
    for node in body:
        if isinstance(node, FUNC):
            q = qual + node.name
            seen[q] = seen.get(q, 0) + 1
            d = defaults(node)
            if d:
                out[q] = d
            walk(node.body, q + ".<locals>.", out, seen)
        elif isinstance(node, ast.ClassDef):
            walk(node.body, qual + node.name + ".", out, seen)
        else:
            for field in ("body", "orelse", "finalbody", "handlers"):
                sub = getattr(node, field, None)
                if isinstance(sub, list):
                    walk(sub, qual, out, seen)


src = Path(sys.argv[1]).resolve()
table = {}
for p in sorted((src / "sqlglot").rglob("*.py")) + sorted((src / "tests").rglob("*.py")):
    rel = str(p.relative_to(src))
    out, seen = {}, {}
    walk(ast.parse(p.read_text(encoding="utf-8")).body, "", out, seen)
    out = {q: d for q, d in out.items() if seen[q] == 1}
    if out:
        table[rel] = out
Path(sys.argv[2]).write_text(json.dumps(table))
print(sum(len(v) for v in table.values()), "functions with literal defaults")
