#!/usr/bin/env python3
"""Validate the staged TPC-DS proof on labeled commits.

usage: stage_validate.py STAGES_DIR DUMP_DIR [JOBS]
A commit qualifies when every changed line is outside the late stages and module-level
code of every entry (and the full test runs no changed line outside the entries); the
entries whose early stages reach a changed line are re-digested on the patched code. A
"same" verdict on a commit where test_execute_tpcds truly fails would be unsound.
"""
import concurrent.futures as cf
import glob
import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BR_SRC", str(ROOT / "work/sqlglot"))
os.environ.setdefault("BR_DATA", "/tmp")
import base_selector as base  # noqa: E402

SETS = {"sample": ROOT / "starter/sample", "wide": ROOT.parent / "blast-radius-oob/data/wide",
        "hot2": ROOT.parent / "blast-radius-oob/data/hot2"}
T = 1106
PY = str(ROOT / "work/venv/bin/python")
CHILD = r'''
import json, signal, sys
src, overlay_file, need_file, out = sys.argv[1:5]
sys.path.insert(0, src); sys.path.insert(1, "%s")
from execrun import _OverlayFinder
sys.meta_path.insert(0, _OverlayFinder(src, json.load(open(overlay_file))))
need = json.load(open(need_file))
res = {}
try:
    import tpcds_harness
    from tests.helpers import load_sql_fixture_pairs
    pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    tables = tpcds_harness.load_tables()
    for i in need:
        signal.alarm(30)
        try:
            res[i] = tpcds_harness.early_digest(pairs[i], tables)
        except BaseException as e:
            res[i] = "error:" + type(e).__name__
        signal.alarm(0)
except BaseException as e:
    res = {"import": "error:" + type(e).__name__}
json.dump(res, open(out, "w"))
''' % ROOT


def load_stages(d):
    late, body, early, pinned = set(), set(), {}, {}
    for f in glob.glob(os.path.join(d, "*.json")):
        r = json.load(open(f))
        pinned[r["index"]] = r["early"]
        for p, ls in r["lines"]["late"].items():
            late |= {(p, l) for l in ls}
        for p, ls in r["lines"]["body"].items():
            body |= {(p, l) for l in ls}
        for p, ls in r["lines"]["early"].items():
            for l in ls:
                early.setdefault((p, l), set()).add(r["index"])
    return late, body, early, pinned


def main():
    stages, dump = sys.argv[1], Path(sys.argv[2])
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    late, body, early, pinned = load_stages(stages)
    cov = base.CoverageMap(base.MAP)
    items = []
    for f in sorted(dump.glob("*.json")):
        for c, v in json.loads(f.read_text()).items():
            if T not in v["sel"]:
                continue
            d = SETS.get(f.stem, ROOT / "work" / f.stem)
            overlay, need, why = {}, set(), None
            for fp in base.parse_patch((d / "commits" / f"{c}.patch").read_text(errors="replace")):
                path = fp.old_path or fp.new_path
                if not path or fp.binary or fp.old_path != fp.new_path or not path.startswith("sqlglot/") or not path.endswith(".py"):
                    why = "non-sqlglot"
                    break
                old = (base.SRC / path).read_text()
                new, removed, _, anchors = base.apply_hunks(old, fp.hunks)
                overlay[path] = new
                for ln in set(removed) | {a for p in anchors for a in (p, p + 1)}:
                    k = (path, ln)
                    if k in late or k in body:
                        why = "late-or-body"
                    elif k in early:
                        need |= early[k]
                    elif (cov.at_line(path, ln) >> T) & 1:
                        why = "test-only-line"
            if why is None and not need:
                why = "no-entry"
            items.append((f.stem, c, T in v["failing"], why, overlay, sorted(need)))

    def run(item):
        s, c, fails, why, overlay, need = item
        if why:
            return s, c, fails, why, None
        with tempfile.TemporaryDirectory() as tmp:
            of, nf, out = (os.path.join(tmp, x) for x in ("o.json", "n.json", "r.json"))
            json.dump(overlay, open(of, "w"))
            json.dump(need, open(nf, "w"))
            try:
                subprocess.run([PY, "-c", CHILD, str(base.SRC), of, nf, out], capture_output=True, timeout=600)
                res = json.load(open(out))
            except Exception:
                return s, c, fails, "crash", None
        same = "import" not in res and all(res.get(str(i)) == pinned[i] for i in need)
        return s, c, fails, "same" if same else "differs", len(need)

    counts = {}
    unsound = []
    with cf.ThreadPoolExecutor(jobs) as ex:
        for s, c, fails, verdict, n in ex.map(run, items):
            key = (verdict, "fails" if fails else "passes")
            counts[key] = counts.get(key, 0) + 1
            if verdict == "same" and fails:
                unsound.append(f"{s}/{c}")
            if verdict in ("same", "differs"):
                print(s, c, verdict, "tpcds fails" if fails else "tpcds passes", n, flush=True)
    for k, v in sorted(counts.items()):
        print(k, v)
    print("UNSOUND:", unsound)


if __name__ == "__main__":
    main()
