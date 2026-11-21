#!/usr/bin/env python3
"""Run the selector on local datasets and score it with the official scorer.

usage: evaluate.py [DATASET_DIR ...] [--verbose] [--selector selector.py]

A dataset dir holds commits/*.patch and answers.json ({"commits": {id: {"failing": [...]}}});
tests.json comes from the starter sample. Commits without an answer are skipped.
Runs the selector natively with Python 3.13 (BR_SRC=work/sqlglot) and reports
simulated seconds, misses, CPU seconds, and the worst commits.
"""
import argparse
import importlib.util
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_JSON = ROOT / "starter/sample/tests.json"
PY = os.environ.get("BR_PY") or str(Path.home() / ".local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13")

spec = importlib.util.spec_from_file_location("score", ROOT / "starter/score.py")
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)


def run(dataset: Path, selector: Path, verbose: bool, dump=None):
    answers = json.loads((dataset / "answers.json").read_text())["commits"]
    answers = {c: v for c, v in answers.items() if "failing" in v and "error" not in v and "incomplete" not in v}
    kinds = {}
    if (dataset / "kinds.json").exists():
        kinds = json.loads((dataset / "kinds.json").read_text())
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "commits").mkdir()
        for c in answers:
            shutil.copy(dataset / "commits" / f"{c}.patch", tmp / "commits" / f"{c}.patch")
        shutil.copy(TESTS_JSON, tmp / "tests.json")
        env = dict(os.environ, BR_DATA=str(tmp), BR_SRC=str(ROOT / "work/sqlglot"),
                   BR_MAP=str(ROOT / "map/cov.json.gz"))
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        p = subprocess.run([PY, "-I", str(selector)], env=env, capture_output=True)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        if p.returncode:
            print(p.stderr.decode()[-3000:])
            raise SystemExit("selector failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    tests = json.loads(TESTS_JSON.read_text())["tests"]
    ev = score.evaluate_run(p.stdout, tests, answers)
    if not ev["ok"]:
        raise SystemExit(f"rejected: {ev}")
    dur = {t["number"]: score._duration_milliseconds(t["seconds"]) for t in tests}
    rows = []
    stderr = {}
    for line in p.stderr.decode().splitlines():
        if ":" in line:
            stderr.setdefault(line.split(":", 1)[0], line)
    for line in p.stdout.decode().splitlines():
        d = json.loads(line)
        ws = [w for w in d["workers"] if w]
        cost = (max(sum(dur[t] for t in w) for w in ws) + 2000 * len(ws)) if ws else 0
        sel = {t for w in ws for t in w}
        fail = answers[d["commit"]]["failing"]
        missed = [t for t in fail if t not in sel]
        fails = bool(fail)
        oracle = 0
        if fail:
            fs = sorted(fail, key=lambda t: -dur[t])
            oracle = min(max(dur[fs[0]], sum(dur[t] for t in fs) / k) + 2000 * k for k in range(1, 9))
        rows.append((d["commit"], cost, missed, len(sel), oracle, kinds.get(d["commit"], answers[d["commit"]].get("kind", "?"))))
    if dump:
        sels = {json.loads(l)["commit"]: sorted({t for w in json.loads(l)["workers"] for t in w})
                for l in p.stdout.decode().splitlines()}
        (dump / f"{dataset.name}.json").write_text(json.dumps(
            {c: {"sel": sels[c], "failing": answers[c]["failing"], "kind": kinds.get(c, answers[c].get("kind", "?")),
                 "log": stderr.get(c, "")} for c in sels}))
    sim = ev["simulated_milliseconds"] / 1000
    n = len(rows)
    print(f"{dataset.name}: n={n} simulated={sim:.1f}s misses={len(ev['missed_commits'])} cpu={cpu:.2f}s "
          f"total={sim + cpu:.1f}s  per-commit={(sim + cpu) / n:.2f}s  oracle={sum(r[4] for r in rows) / 1000:.1f}s")
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r[5], [0, 0, 0, 0])
        k[0] += 1
        k[1] += r[1] / 1000
        k[2] += bool(r[2])
        k[3] += r[4] / 1000
    for k, (cnt, cost, miss, orc) in sorted(by_kind.items()):
        print(f"   {k:11s} n={cnt:3d} cost={cost:8.1f} oracle={orc:7.1f} misses={miss}")
    for r in rows:
        if r[2]:
            print(f"   MISS {r[0]} kind={r[5]} missed={[tests[t]['id'] for t in r[2][:4]]} | {stderr.get(r[0], '')[:300]}")
    if verbose:
        for r in sorted(rows, key=lambda r: -(r[1] - r[4]))[:25]:
            print(f"   {r[0]} cost={r[1] / 1000:.1f} oracle={r[4] / 1000:.1f} sel={r[3]} kind={r[5]} | {stderr.get(r[0], '')[:250]}")
    return sim + cpu, len(ev["missed_commits"]), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("datasets", nargs="*")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--selector", default=str(ROOT / "selector.py"))
    ap.add_argument("--dump", type=Path, help="write each set's selections to DUMP/<set>.json")
    a = ap.parse_args()
    ds = a.datasets or [str(ROOT / "starter/sample")]
    for d in ds:
        run(Path(d), Path(a.selector), a.verbose, a.dump)


if __name__ == "__main__":
    main()
