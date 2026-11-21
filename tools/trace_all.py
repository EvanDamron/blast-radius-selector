#!/usr/bin/env python3
"""Run inside br-env: every test in its own pytest process under the tracer.

usage: python trace_all.py TESTS_JSON OUT_DIR [JOBS] [SRC_DIR]
Writes OUT_DIR/<number>/<pid>.json per process and OUT_DIR/<number>.rc.
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 8
src = os.path.abspath(sys.argv[4]) if len(sys.argv) > 4 else "/src"
tools = os.path.dirname(os.path.abspath(__file__))
order = sorted(tests, key=lambda t: -t["seconds"])  # long ones first


def run(t):
    d = os.path.join(out, str(t["number"]))
    if os.path.exists(d + ".rc"):
        return t["number"], "cached"
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ, BRTRACE_DIR=os.path.abspath(d), PYTHONPATH=f"{src}:" + os.environ.get("BR_HOOK_DIR", f"{tools}/trace"),
               BRTRACE_PREFIX=src + "/", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-qq", "-x",
                        "-p", "no:xdist", t["id"]], env=env, capture_output=True, text=True, cwd=src)
    open(d + ".rc", "w").write(str(r.returncode))
    return t["number"], r.returncode


with cf.ThreadPoolExecutor(jobs) as ex:
    for i, (n, rc) in enumerate(ex.map(run, order)):
        if rc not in (0, "cached"):
            print("rc", n, rc, flush=True)
        if i % 100 == 0:
            print(i, flush=True)
