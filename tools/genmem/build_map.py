#!/usr/bin/env python3
"""map/genmem.json.gz from per-dialect traces (trace_dialects.py).

usage: build_map.py TRACE_DIR OUT.json.gz
{"names": [dialect value, ...], "ms": [cpu ms, ...],
 "lines": {path: {line: [dialect index, ...]}}}   # function and import-time code alike
"""
import glob
import gzip
import json
import os
import sys

trace, out = sys.argv[1], sys.argv[2]
names, ms, lines = [], [], {}
i = 0
while os.path.exists(os.path.join(trace, f"{i}.result")):
    r = json.load(open(os.path.join(trace, f"{i}.result")))
    assert r["ok"], r
    names.append(r["name"])
    ms.append(r["ms"])
    for f in glob.glob(os.path.join(trace, str(i), "*.json")):
        for path, ls in json.load(open(f))["lines"].items():
            d = lines.setdefault(path[1:] if path.startswith("B") else path, {})
            for line in ls:
                d.setdefault(line, set()).add(i)
    i += 1
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump({"names": names, "ms": ms,
               "lines": {p: {str(l): sorted(e) for l, e in d.items()} for p, d in lines.items()}}, fh, separators=(",", ":"))
print(len(names), "dialects", sum(len(d) for d in lines.values()), "lines")
