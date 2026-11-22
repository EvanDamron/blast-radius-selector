#!/usr/bin/env python3
"""map/tpcds_stages.json.gz from trace_stages.py output.

usage: build_stage_map.py STAGES_DIR OUT.json.gz
{"early_digest": [per entry], "early_ms": [per entry],
 "early": {path: {line: [entry, ...]}},   # lines an entry's early stages run
 "late": {path: [line, ...]},             # lines any entry's late stages run
 "body": {path: [line, ...]}}             # module/class-level lines any entry runs
"""
import glob
import gzip
import json
import os
import sys

d, out = sys.argv[1], sys.argv[2]
recs = sorted((json.load(open(f)) for f in glob.glob(os.path.join(d, "*.json"))), key=lambda r: r["index"])
assert [r["index"] for r in recs] == list(range(len(recs)))
early, late, body = {}, {}, {}
for r in recs:
    for p, ls in r["lines"]["early"].items():
        e = early.setdefault(p, {})
        for l in ls:
            e.setdefault(str(l), []).append(r["index"])
    for k, acc in (("late", late), ("body", body)):
        for p, ls in r["lines"][k].items():
            acc.setdefault(p, set()).update(ls)
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump({"early_digest": [r["early"] for r in recs], "early_ms": [r["early_ms"] for r in recs],
               "early": early, "late": {p: sorted(s) for p, s in late.items()},
               "body": {p: sorted(s) for p, s in body.items()}}, fh, separators=(",", ":"))
print(len(recs), "entries")
