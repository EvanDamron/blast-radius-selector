#!/usr/bin/env python3
"""map/tpcds.json.gz from per-entry traces (trace_entries.py) and digests (run_query.py).

usage: build_map.py TRACE_DIR DIGESTS.json OUT.json.gz
{"entries": [[execute, digest, warm cpu ms], ...],
 "lines": {path: {line: [entry, ...]}},   # function code each entry ran
 "body": {path: [line, ...]}}             # module/class-level code any entry ran
"""
import glob
import gzip
import json
import os
import sys

trace, digests, out = sys.argv[1], json.load(open(sys.argv[2])), sys.argv[3]
n = len(digests)
entries, lines, body = [], {}, {}
for i in range(n):
    digest, ms = digests[str(i)]
    entries.append([digest != "parsed", digest, ms])
    for f in glob.glob(os.path.join(trace, str(i), "*.json")):
        for path, ls in json.load(open(f))["lines"].items():
            if path.startswith("B"):
                body.setdefault(path[1:], set()).update(ls)
            else:
                d = lines.setdefault(path, {})
                for line in ls:
                    d.setdefault(line, set()).add(i)
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump({"entries": entries,
               "lines": {p: {str(l): sorted(e) for l, e in d.items()} for p, d in lines.items()},
               "body": {p: sorted(s) for p, s in body.items()}}, fh, separators=(",", ":"))
print(n, "entries", sum(len(d) for d in lines.values()), "function lines")
