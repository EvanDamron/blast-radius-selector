#!/usr/bin/env python3
"""For labeled commits where test_execute_tpcds is a candidate, run every TPC-DS entry on
the patched code and record which digests differ from the pinned ones.

usage: digest_sweep.py DUMP_DIR OUT.json [JOBS]
DUMP_DIR holds evaluate.py --dump output (one <set>.json per labeled set); patches are
read from the set directories. Only commits whose changes are all Python files run.
"""
import concurrent.futures as cf
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
TPCDS = 1106
PY = str(ROOT / "work/venv/bin/python")
CHILD = r'''
import json, signal, sys
src, overlay_file, out = sys.argv[1:4]
def _alarm(*a):
    raise TimeoutError()
signal.signal(signal.SIGALRM, _alarm)
sys.path.insert(0, src); sys.path.insert(1, "%s")
from execrun import _OverlayFinder
sys.meta_path.insert(0, _OverlayFinder(src, json.load(open(overlay_file))))
res = {}
try:
    import tpcds_harness
    from tests.helpers import load_sql_fixture_pairs
    pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    tables = tpcds_harness.load_tables()
    for i, p in enumerate(pairs):
        try:
            signal.alarm(30)
            res[i] = tpcds_harness.run_entry(p, tables)
        except BaseException as e:
            res[i] = "error:" + type(e).__name__
        finally:
            signal.alarm(0)
except BaseException as e:
    res = {"import": "error:" + type(e).__name__}
json.dump(res, open(out, "w"))
''' % ROOT


def work(item):
    s, c, failing = item
    d = SETS.get(s, ROOT / "work" / s)
    text = (d / "commits" / f"{c}.patch").read_text(encoding="utf-8", errors="replace")
    overlay = {}
    for fp in base.parse_patch(text):
        path = fp.old_path or fp.new_path
        if not path or fp.binary or fp.old_path != fp.new_path or not path.endswith(".py"):
            return s, c, failing, None
        old = (base.SRC / path).read_text(encoding="utf-8")
        overlay[path] = base.apply_hunks(old, fp.hunks)[0]
    with tempfile.TemporaryDirectory() as tmp:
        of, out = os.path.join(tmp, "o.json"), os.path.join(tmp, "r.json")
        json.dump(overlay, open(of, "w"))
        try:
            subprocess.run([PY, "-c", CHILD, str(base.SRC), of, out], capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            return s, c, failing, {"import": "error:timeout"}
        try:
            return s, c, failing, json.load(open(out))
        except Exception:
            return s, c, failing, {"import": "error:crash"}


def main():
    dump, outp = Path(sys.argv[1]), sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    items = []
    for f in sorted(dump.glob("*.json")):
        for c, v in json.loads(f.read_text()).items():
            if TPCDS in v["sel"]:
                items.append((f.stem, c, TPCDS in v["failing"]))
    part = outp + ".part"
    result = {}
    if os.path.exists(part):
        for line in open(part):
            k, v = json.loads(line)
            result[k] = v
    items = [it for it in items if f"{it[0]}/{it[1]}" not in result]
    with cf.ThreadPoolExecutor(jobs) as ex, open(part, "a") as fh:
        for s, c, failing, digests in ex.map(work, items):
            v = {"fails": failing, "digests": digests}  # digests None: not all-Python, skipped
            result[f"{s}/{c}"] = v
            fh.write(json.dumps([f"{s}/{c}", v]) + "\n")
            fh.flush()
            print(s, c, failing, "skip" if digests is None else len(digests), flush=True)
    json.dump({k: v for k, v in result.items() if v["digests"] is not None}, open(outp, "w"))


if __name__ == "__main__":
    main()
