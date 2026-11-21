#!/usr/bin/env python3
"""Fold per-test traces (TRACE_DIR/<number>/<pid>.json, any number of dirs, unioned)
into map/cov.json.gz.

Format: {"n": N, "sets": [hex bitset, ...],
         "lines": {relpath: {line: set_index}},   # lines run by function code
         "body": {relpath: {line: set_index}},    # lines run by module/class-body code
         "opened": {relpath: set_index},
         "classes": {"module:Qualname": set_index}}  # expression classes instantiated
A bitset has bit t set when test number t executed that line / opened that file.
Paths are relative to the sqlglot checkout root.
"""
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
traces = [Path(a) for a in sys.argv[1:]] or [ROOT / "work" / "trace"]
tests = json.loads((ROOT / "starter/sample/tests.json").read_text())["tests"]
N = len(tests)

line_bits: dict = {}
body_bits: dict = {}
class_bits: dict = {}
open_bits: dict = {}
missing = []
for t in tests:
    n = t["number"]
    files = []
    for trace in traces:
        d = trace / str(n)
        rc = trace / f"{n}.rc"
        got = list(d.glob("*.json")) if d.exists() else []
        files += got
        if not got or not rc.exists() or rc.read_text().strip() != "0":
            missing.append((n, t["id"], str(trace), rc.read_text() if rc.exists() else None, len(got)))
    bit = 1 << n
    for f in files:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            missing.append((n, t["id"], "badjson", str(f)))
            continue
        for path, lines in data["lines"].items():
            body = path.startswith("B")
            rel = path[body:]
            m = (body_bits if body else line_bits).setdefault(rel, {})
            for ln in lines:
                m[ln] = m.get(ln, 0) | bit
        for name in data.get("classes", []):
            class_bits[name] = class_bits.get(name, 0) | bit
        for path in data["opened"]:
            rel = path
            open_bits[rel] = open_bits.get(rel, 0) | bit

sets: list = []
index: dict = {}


def sid(b: int) -> int:
    i = index.get(b)
    if i is None:
        i = index[b] = len(sets)
        sets.append(format(b, "x"))
    return i


out = {
    "n": N,
    "lines": {f: {str(ln): sid(b) for ln, b in sorted(m.items())} for f, m in sorted(line_bits.items())},
    "body": {f: {str(ln): sid(b) for ln, b in sorted(m.items())} for f, m in sorted(body_bits.items())},
    "opened": {f: sid(b) for f, b in sorted(open_bits.items())},
    "classes": {c: sid(b) for c, b in sorted(class_bits.items())},
}
out["sets"] = sets
blob = json.dumps(out, separators=(",", ":")).encode()
(ROOT / "map").mkdir(exist_ok=True)
with gzip.open(ROOT / "map" / "cov.json.gz", "wb", compresslevel=9) as fh:
    fh.write(blob)
print(f"files={len(line_bits)} lines={sum(len(m) for m in line_bits.values())} sets={len(sets)} "
      f"opened={len(open_bits)} raw={len(blob)/1e6:.1f}MB gz={(ROOT/'map'/'cov.json.gz').stat().st_size/1e6:.1f}MB")
print("problems:", len(missing))
for m in missing[:30]:
    print("  ", m)
