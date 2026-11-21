#!/usr/bin/env python3
"""Merge per-test default records into map/defaults.json.gz.

usage: merge.py TESTS_JSON TRACE_DIR SQLGLOT_DIR MAP_DIR
MAP_DIR/defaults.json.gz: {"sets": [hex bitset, ...], "keys": {relpath: {qualname: {param: set index}}}}
MAP_DIR/argtypes.json.gz: {"sets": [...], "keys": {class key: {arg: set index}},
                           "spreads": {class key: [class keys whose arg_types copy its arg_types]}}
A test whose run failed (nonzero .rc) is fatal: its record would be incomplete.
"""
import ast
import glob
import gzip
import json
import os
import sys

tests = json.load(open(sys.argv[1]))["tests"]
trace = sys.argv[2]
bits: dict = {}
missing: dict = {}
for t in tests:
    n = t["number"]
    rc = open(os.path.join(trace, f"{n}.rc")).read().strip()
    if rc not in ("0", "5"):
        sys.exit(f"test {n} ({t['id']}) exited {rc}")
    for f in glob.glob(os.path.join(trace, str(n), "*.json")):
        rec = json.load(open(f))
        for rel, qual, param in rec["maybe_default"]:
            k = (rel, qual, param)
            bits[k] = bits.get(k, 0) | (1 << n)
        for cls, arg in rec["missing_args"]:
            k = (cls, arg)
            missing[k] = missing.get(k, 0) | (1 << n)


def write(path, records, extra=None):
    sets, index, keys = [], {}, {}
    for k, b in sorted(records.items()):
        h = format(b, "x")
        if h not in index:
            index[h] = len(sets)
            sets.append(h)
        d = keys
        for part in k[:-1]:
            d = d.setdefault(part, {})
        d[k[-1]] = index[h]
    with gzip.open(path, "wt", compresslevel=9) as fh:
        json.dump({"sets": sets, "keys": keys, **(extra or {})}, fh, separators=(",", ":"))
    print(path, len(records), "records,", len(sets), "distinct sets")


# Classes whose arg_types copy another class's (`{**Cast.arg_types, ...}`): a value
# changed in Cast's literal also changes theirs. Names resolve within sqlglot.expressions.
src = os.path.abspath(sys.argv[3])
classes: dict = {}
copies = []
for p in sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)):
    mod = os.path.relpath(p, src)[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]

    def walk(body, qual):
        for node in body:
            if isinstance(node, ast.ClassDef):
                key = f"{mod}:{'.'.join(qual + [node.name])}"
                classes.setdefault(node.name, []).append(key)
                for st in node.body:
                    target = st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else getattr(st, "target", None)
                    if isinstance(target, ast.Name) and target.id == "arg_types" and getattr(st, "value", None) is not None:
                        for sub in ast.walk(st.value):
                            if isinstance(sub, ast.Attribute) and sub.attr == "arg_types":
                                base = sub.value
                                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
                                copies.append((name, key))
                walk(node.body, qual + [node.name])

    walk(ast.parse(open(p, encoding="utf-8").read()).body, [])
spreads: dict = {}
for name, key in copies:
    for src_key in classes.get(name, []):
        spreads.setdefault(src_key, []).append(key)

out = sys.argv[4]
write(os.path.join(out, "defaults.json.gz"), bits)
write(os.path.join(out, "argtypes.json.gz"), missing, {"spreads": spreads})
print("spreads:", spreads)
