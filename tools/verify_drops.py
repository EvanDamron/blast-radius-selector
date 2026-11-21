#!/usr/bin/env python3
"""Check a narrowing stage by running every test it drops against the patched code.

usage: verify_drops.py OUT_DIR COUNT_PER_KIND [--seed N] [--stage-env NAME]

Generates edits the stage targets (arg_types value flips, literal default changes),
runs the selector with the stage off (NAME=0) and on, applies each patch to a
scratch copy of the pinned tree, and runs the dropped tests there. Any failure is
a stage error. Writes OUT_DIR/report.json.
"""
import argparse
import ast
import difflib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "work" / "sqlglot"
PY = ROOT / "work" / "venv" / "bin" / "python"
SEL_PY = os.environ.get("BR_PY") or str(Path.home() / ".local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13")


def diff(rel, old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))


def replace_at(text, node, new_src):
    lines = text.splitlines(True)
    i = node.lineno - 1
    if node.end_lineno != node.lineno:
        return None
    line = lines[i]
    lines[i] = line[: node.col_offset] + new_src + line[node.end_col_offset:]
    return "".join(lines)


def argtypes_mutants(rng, n):
    sites = []
    for p in sorted((SRC / "sqlglot" / "expressions").rglob("*.py")):
        text = p.read_text()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign) and getattr(st.targets[0], "id", None) == "arg_types" and isinstance(st.value, ast.Dict):
                        for v in st.value.values:
                            if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                                sites.append((p, v))
    out = []
    for p, v in rng.sample(sites, min(n, len(sites))):
        old = p.read_text()
        new = replace_at(old, v, str(not v.value))
        if new:
            out.append(("argtypes", diff(str(p.relative_to(SRC)), old, new)))
    return out


def default_mutants(rng, n):
    sites = []
    for p in sorted((SRC / "sqlglot").rglob("*.py")):
        text = p.read_text()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    if isinstance(d, ast.Constant) and type(d.value) in (bool, int, str) and d.value is not None:
                        sites.append((p, d))
    out = []
    for p, d in rng.sample(sites, min(n, len(sites))):
        v = d.value
        if isinstance(v, bool):
            src = str(not v)
        elif isinstance(v, int):
            src = str(v + rng.choice([-1, 1]))
        else:
            src = repr(v + "x")
        old = p.read_text()
        new = replace_at(old, d, src)
        if new:
            out.append(("default", diff(str(p.relative_to(SRC)), old, new)))
    return out


def fixture_mutants(rng, n):
    """Generator-like corruptions of lines in SQL fixtures that several tests open."""
    files = ["tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
             "tests/fixtures/identity.sql", "tests/fixtures/pretty.sql"]
    out = []
    for _ in range(n):
        rel = rng.choice(files)
        old = (SRC / rel).read_text()
        lines = old.splitlines(True)
        i = rng.randrange(len(lines))
        line = lines[i]
        how = rng.choice(["insert", "comment", "delete", "swap"])
        if how == "insert":
            words = line.split(" ")
            j = rng.randrange(len(words))
            words.insert(j, rng.choice(["42", "0", "x"]))
            line = " ".join(words)
        elif how == "comment":
            line = "-- note\n" + line
        elif how == "delete" and len(line.strip()) > 2:
            j = rng.randrange(len(line.rstrip("\n")))
            line = line[:j] + line[j + 1:]
        else:
            words = line.rstrip("\n").split(" ")
            if len(words) > 2:
                j = rng.randrange(len(words) - 1)
                words[j], words[j + 1] = words[j + 1], words[j]
            line = " ".join(words) + "\n"
        lines[i] = line
        new = "".join(lines)
        if new != old:
            out.append(("fixture", diff(rel, old, new)))
    return out


STAGES = ("BR_USE_DEFAULTS", "BR_USE_FIXTURES")


def select(data, env_name, on):
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(SRC), BR_MAP=str(ROOT / "map/cov.json.gz"))
    for name in (STAGES if env_name == "all" else (env_name,)):
        env[name] = "1" if on else "0"
    p = subprocess.run([SEL_PY, "-I", str(ROOT / "selector.py")], env=env, capture_output=True, text=True, check=True)
    return {json.loads(l)["commit"]: {t for w in json.loads(l)["workers"] for t in w} for l in p.stdout.splitlines()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("count", type=int)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--stage-env", default="all")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    muts = argtypes_mutants(rng, a.count) + default_mutants(rng, a.count) + fixture_mutants(rng, a.count)
    kinds = {}
    for i, (kind, patch) in enumerate(muts):
        cid = f"v{i:03d}"
        (out / "commits" / f"{cid}.patch").write_text(patch)
        kinds[cid] = kind
    shutil.copy(ROOT / "starter/sample/tests.json", out / "tests.json")
    tests = json.loads((out / "tests.json").read_text())["tests"]
    off = select(out, a.stage_env, False)
    on = select(out, a.stage_env, True)
    scratch = out / "src"
    if not scratch.exists():
        shutil.copytree(SRC, scratch, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    env = dict(os.environ, PATH=f"{PY.parent}:{os.environ['PATH']}", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2",
               PYTHONDONTWRITEBYTECODE="1")
    report = {}
    for cid in sorted(kinds):
        dropped = sorted(off[cid] - on[cid])
        rec = {"kind": kinds[cid], "base": len(off[cid]), "kept": len(on[cid]), "dropped": len(dropped), "failed": []}
        if dropped:
            patch = out / "commits" / f"{cid}.patch"
            subprocess.run(["patch", "-p1", "-s", "-i", str(patch.resolve())], cwd=scratch, check=True)
            try:
                ids = [tests[n]["id"] for n in dropped]
                r = subprocess.run([str(PY), "-m", "pytest", "-p", "no:cacheprovider", "-q", "-p", "no:xdist", "-rf",
                                    *ids], cwd=scratch, env=env, capture_output=True, text=True)
                if r.returncode not in (0, 5):
                    rec["failed"] = [l for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))][:20] or [r.stdout[-500:]]
            finally:
                subprocess.run(["patch", "-p1", "-R", "-s", "-i", str(patch.resolve())], cwd=scratch, check=True)
        report[cid] = rec
        print(cid, rec["kind"], rec["base"], "->", rec["kept"], "FAIL" if rec["failed"] else "ok", flush=True)
        (out / "report.json").write_text(json.dumps(report, indent=1))
    bad = [c for c, r in report.items() if r["failed"]]
    print("mutants", len(report), "narrowed", sum(1 for r in report.values() if r["dropped"]), "errors", len(bad), bad)


if __name__ == "__main__":
    main()
