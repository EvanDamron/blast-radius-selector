#!/usr/bin/env python3
"""Label patches: apply each to a copy of pinned sqlglot, run the full suite in br-env,
and write {"commits": {id: {"failing": [...]}}} like answers.json.

usage: label.py OUT.json PATCH... [--jobs N] [--workers N]
"""
import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "work" / "sqlglot"
TESTS = json.loads((ROOT / "starter/sample/tests.json").read_text())["tests"]
BY_ID = {t["id"]: t["number"] for t in TESTS}
BY_FILE = {}
for t in TESTS:
    BY_FILE.setdefault(t["file"], []).append(t["number"])


NATIVE = False


def label(patch: Path, workers: int, cpuset: str = "") -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="lab-", dir=ROOT / "work" / "tmp"))
    try:
        tree = scratch / "src"
        shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests", "benchmarks", "posts", "pdoc"))
        r = subprocess.run(["patch", "-p1", "-s", "-f", "--no-backup-if-mismatch", "-i", str(patch)], cwd=tree, capture_output=True, text=True)
        if r.returncode:
            return {"error": "apply failed: " + r.stderr + r.stdout}
        out = scratch / "out"
        out.mkdir()
        if NATIVE:
            env = dict(os.environ, BRLABEL_OUT=str(out / "r"), PYTHONPATH=f"{tree}:{ROOT/'tools'}",
                       SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2")
            env.pop("VIRTUAL_ENV", None)
            subprocess.run([str(ROOT / "work/venv/bin/python"), "-m", "pytest", "-p", "brlabel", "-p", "no:cacheprovider",
                            "-qq", "-n", str(workers), "--timeout", "300", "tests"], cwd=tree, env=env,
                           capture_output=True, text=True)
        cmd = ["docker", "run", "--rm"] + (["--cpuset-cpus", cpuset] if cpuset else []) + ["-v", f"{tree}:/src:ro", "-v", f"{ROOT/'tools'}:/tools:ro",
               "-v", f"{out}:/out", "-e", "BRLABEL_OUT=/out/r", "-e", "PYTHONPATH=/src:/tools",
               "br-env", "python", "-m", "pytest", "-p", "brlabel", "-p", "no:cacheprovider",
               "-qq", "-n", str(workers), "--timeout", "300", "tests"]
        if not NATIVE:
            subprocess.run(cmd, capture_output=True, text=True)
        status, errors = {}, []
        for f in out.glob("r.*.json"):
            d = json.loads(f.read_text())
            for k, v in d["status"].items():
                if v == "fail" or k not in status:
                    status[k] = v
            errors += d["collect_errors"]
        failing = set()
        unknown = []
        for nodeid, v in status.items():
            if v == "fail":
                if nodeid in BY_ID:
                    failing.add(BY_ID[nodeid])
                else:
                    unknown.append(nodeid)
        for e in errors:
            for n in BY_FILE.get(e, []):
                failing.add(n)
        seen = sum(1 for k in status if k in BY_ID)
        res = {"failing": sorted(failing)}
        if unknown or errors:
            res["note"] = {"unknown": unknown[:20], "collect_errors": errors}
        if seen < len(TESTS) and not errors:
            res["incomplete"] = seen
        return res
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("patches", nargs="+")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--native", action="store_true")
    a = ap.parse_args()
    global NATIVE
    NATIVE = a.native
    (ROOT / "work" / "tmp").mkdir(parents=True, exist_ok=True)
    outp = Path(a.out)
    result = json.loads(outp.read_text()) if outp.exists() else {"commits": {}}
    todo = [Path(p).resolve() for p in a.patches if Path(p).stem not in result["commits"]]
    import queue
    slots = queue.Queue()
    per = 14 // a.jobs
    for j in range(a.jobs):
        slots.put(f"{j*per}-{j*per+per-1}" if a.jobs > 1 else "")

    def job(p):
        s = slots.get()
        try:
            for attempt in range(3):
                res = label(p, a.workers, s)
                if "incomplete" not in res and "error" not in res:
                    return res
                print(p.stem, "retrying:", res.get("incomplete"), res.get("error", "")[:200], file=sys.stderr, flush=True)
            return res
        finally:
            slots.put(s)

    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(job, p): p for p in todo}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            res = fut.result()
            result["commits"][p.stem] = res
            print(p.stem, len(res.get("failing", [])), res.get("note", ""), res.get("error", ""), res.get("incomplete", ""), file=sys.stderr, flush=True)
            outp.write_text(json.dumps(result, indent=0))


if __name__ == "__main__":
    main()
