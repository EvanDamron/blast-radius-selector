#!/usr/bin/env python3
"""Merge per-test module attribute records into map/modattr.json.gz.

usage: merge.py TESTS_JSON TRACE_DIR[,TRACE_DIR...] OUT.json.gz
{"sets": [hex bitset, ...], "keys": {module: {name: set index}}}
A test whose run failed is fatal: its record would be incomplete.
"""
import glob
import gzip
import json
import os
import sys

tests = json.load(open(sys.argv[1]))["tests"]
bits: dict = {}
for trace in sys.argv[2].split(","):
    for t in tests:
        n = t["number"]
        rc = open(os.path.join(trace, f"{n}.rc")).read().strip()
        if rc not in ("0", "5"):
            sys.exit(f"test {n} ({t['id']}) exited {rc} in {trace}")
        for f in glob.glob(os.path.join(trace, str(n), "*.json")):
            for module, name in json.load(open(f))["modattr"]:
                k = (module, name)
                bits[k] = bits.get(k, 0) | (1 << n)
sets, index, keys = [], {}, {}
for (module, name), b in sorted(bits.items()):
    h = format(b, "x")
    if h not in index:
        index[h] = len(sets)
        sets.append(h)
    keys.setdefault(module, {})[name] = index[h]
with gzip.open(sys.argv[3], "wt", compresslevel=9) as fh:
    json.dump({"sets": sets, "keys": keys}, fh, separators=(",", ":"))
print(len(bits), "records,", len(sets), "distinct sets")
