#!/usr/bin/env python3
"""Run every (test, SQL fixture it opens, kind) with that part poisoned.

usage: poison_all.py TESTS_JSON COV_MAP SRC_DIR OUT.json [JOBS]
Writes {fixture relpath: {kind: [numbers of tests that read it]}} for every SQL
fixture some test opens; kind "direct" lists tests that open the file other than
through tests/helpers.py. A failing run counts as a read.
"""
import concurrent.futures as cf
import gzip
import json
import os
import subprocess
import sys
import tempfile

tests = json.load(open(sys.argv[1]))["tests"]
cov = json.load(gzip.open(sys.argv[2]))
src = os.path.abspath(sys.argv[3])
out = sys.argv[4]
jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 4
hook = os.path.dirname(os.path.abspath(__file__))
KINDS = ("meta", "sql", "expected", "entry")

work = []
for f, i in cov["opened"].items():
    if f.startswith("tests/fixtures/") and f.endswith(".sql"):
        b = int(cov["sets"][i], 16)
        for t in tests:
            if b >> t["number"] & 1:
                for k in KINDS:
                    work.append((t, f, k))


def run(item):
    t, f, k = item
    fd, mark = tempfile.mkstemp(prefix="brpoison", dir=os.environ.get("BRPOISON_TMP"))
    os.close(fd)
    os.unlink(mark)
    env = dict(os.environ, PYTHONPATH=f"{src}:{hook}", BRPOISON=f"{f[len('tests/fixtures/'):]}|{k}",
               BRPOISON_MARK=mark, SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-qq", "-x", "-p", "no:xdist",
                          t["id"]], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=src,
                         start_new_session=True)
    try:
        rc = p.wait(timeout=120 + 10 * t["seconds"])
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.wait()
        rc = -9  # a hang counts as a read

    class R:
        returncode = rc

    r = R()
    direct = False
    if os.path.exists(mark):
        direct = "direct open" in open(mark).read()
        os.unlink(mark)
        used = True
    else:
        used = r.returncode != 0
    return t["number"], f, k, used, direct


result: dict = {}
with cf.ThreadPoolExecutor(jobs) as ex:
    for i, (n, f, k, used, direct) in enumerate(ex.map(run, work)):
        kinds = result.setdefault(f, {})
        if used:
            kinds.setdefault(k, []).append(n)
        if direct and n not in kinds.setdefault("direct", []):
            kinds["direct"].append(n)
        if i % 50 == 0:
            print(i, len(work), flush=True)
json.dump(result, open(out, "w"), indent=0)
print("done", len(work))
