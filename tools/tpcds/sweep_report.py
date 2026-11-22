#!/usr/bin/env python3
"""Sampled-entry bet for test_execute_tpcds, from digest_sweep.py output.

usage: sweep_report.py SWEEP.json DUMP_DIR
For each commit: D = entries whose digest differs from the pinned one; A = entries whose
own coverage reaches a changed line (map/tpcds.json.gz). For subset sizes k, S = the k
cheapest entries of A; a run is clean when S and D do not meet.
"""
import gzip
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BR_SRC", str(ROOT / "work/sqlglot"))
os.environ.setdefault("BR_DATA", "/tmp")
import base_selector as base  # noqa: E402

sweep = json.load(open(sys.argv[1]))
tm = json.loads(gzip.decompress((ROOT / "map/tpcds.json.gz").read_bytes()))
pinned = {i: e[1] for i, e in enumerate(tm["entries"])}
ms = {i: e[2] for i, e in enumerate(tm["entries"])}
SETS = {"sample": ROOT / "starter/sample", "wide": ROOT.parent / "blast-radius-oob/data/wide",
        "hot2": ROOT.parent / "blast-radius-oob/data/hot2"}

rows = []
for key, v in sweep.items():
    s, c = key.split("/")
    dg = v["digests"]
    if "import" in dg:
        D = set(range(len(pinned)))  # nothing imports: every entry is affected
    else:
        D = {int(i) for i, d in dg.items() if d != pinned[int(i)]}
    d = SETS.get(s, ROOT / "work" / s)
    A = set()
    for fp in base.parse_patch((d / "commits" / f"{c}.patch").read_text(errors="replace")):
        path = fp.old_path or fp.new_path
        old = (base.SRC / path).read_text()
        _, removed, _, anchors = base.apply_hunks(old, fp.hunks)
        lines = set(removed) | {a for p in anchors for a in (p, p + 1)}
        for ln in lines:
            A |= set(tm["lines"].get(path, {}).get(str(ln), []))
    rows.append((key, v["fails"], D, A))

fails = [r for r in rows if r[1]]
passes = [r for r in rows if not r[1]]
print(f"commits {len(rows)}: tpcds fails {len(fails)}, passes {len(passes)}")
print("fails with no changed digest (harness blind):", [r[0] for r in fails if not r[2]])
print("passes with a changed digest:", sum(1 for r in passes if r[2]))
import statistics
print("failing commits, entries changed: median", statistics.median(len(r[2]) for r in fails),
      "| <=2:", sum(1 for r in fails if len(r[2]) <= 2), "| <=5:", sum(1 for r in fails if len(r[2]) <= 5))
for k in (5, 10, 15, 25):
    q_bad = clean_pass = cost = 0
    for key, f, D, A in rows:
        S = sorted(A, key=lambda e: ms[e])[:k]
        clean = not (set(S) & D) and bool(A)
        cost += 1500 + 5 * sum(ms[e] for e in S)
        if f and clean:
            q_bad += 1
        if not f and clean:
            clean_pass += 1
    print(f"k={k:2d}: false-clean {q_bad}/{len(fails)} (q={q_bad / len(fails):.1%}), clean on passing "
          f"{clean_pass}/{len(passes)}, x86 CPU {cost / 1000:.0f}s over {len(rows)} runs")
