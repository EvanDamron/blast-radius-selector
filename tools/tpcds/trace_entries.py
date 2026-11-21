#!/usr/bin/env python3
"""Coverage of each tpc-ds.sql entry on its own, under tools/trace.

usage: trace_entries.py SRC_DIR OUT_DIR [JOBS]
Writes OUT_DIR/<index>/<pid>.json (coverage) and OUT_DIR/<index>.result.
"""
import concurrent.futures as cf
import os
import subprocess
import sys

src = os.path.abspath(sys.argv[1])
out = os.path.abspath(sys.argv[2])
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 4
tools = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, src)
from tests.helpers import load_sql_fixture_pairs  # noqa: E402

n = len(list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")))


def run(i):
    d = os.path.join(out, str(i))
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ, BRTRACE_DIR=d, BRTRACE_PREFIX=src + "/", PYTHONPATH=f"{tools}/trace",
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, f"{tools}/tpcds/run_query.py", src, d + ".result", str(i)], env=env,
                       capture_output=True, text=True)
    return i, r.returncode, r.stderr[-300:]


with cf.ThreadPoolExecutor(jobs) as ex:
    for i, rc, err in ex.map(run, range(n)):
        if rc:
            print("FAIL", i, rc, err, flush=True)
print("entries", n)
