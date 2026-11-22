"""Merge operand-relation records into map/rel.json.gz.
usage: merge_rel.py RECDIR[,RECDIR2] TESTS_JSON OUT
{"sets": [hex bitsets], "sites": {path: {"line:col": {signature: set}}}}: for each comparison
and each relation signature of its operands (T/F/X/N for ==, !=, <, <=, >, >=, is, is not),
the tests that produced it. A site whose signatures differ between passes is marked "*"."""
import gzip, json, sys
from pathlib import Path

def main():
    recs = [Path(x) for x in sys.argv[1].split(",")]; tests = json.load(open(sys.argv[2]))["tests"]; out = sys.argv[3]
    acc = {}; missing = []
    # comparison sites already known to change with the hash seed (from two recording passes)
    unstable = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else set()
    for t in tests:
        loaded = []
        for r in recs:
            f = r / f"t{t['number']}.json.gz"
            if f.exists(): loaded.append(set(json.load(gzip.open(f, "rt"))))
        if len(loaded) != len(recs): missing.append(t["number"]); continue
        union = set().union(*loaded)
        for x in loaded:
            for k in union - x: unstable.add(k.rsplit("|", 1)[0])
        bit = 1 << t["number"]
        for k in union: acc[k] = acc.get(k, 0) | bit
    if missing: raise SystemExit(f"missing {len(missing)} tests: {missing[:10]}")
    sets = []; index = {}
    def idx(b):
        if b not in index: index[b] = len(sets); sets.append(format(b, "x"))
        return index[b]
    sites = {}
    for k, b in sorted(acc.items()):
        site, sig = k.rsplit("|", 1); path, line, col = site.rsplit(":", 2)
        e = sites.setdefault(path, {}).setdefault(f"{line}:{col}", {})
        e[sig] = idx(b)
    for site in unstable:
        path, line, col = site.rsplit(":", 2)
        e = sites[path][f"{line}:{col}"]
        allb = 0
        for sig, i in e.items():
            if sig != "*": allb |= int(sets[i], 16)
        e.clear(); e["*"] = idx(allb)
    with gzip.open(out, "wb", compresslevel=9) as fh:
        fh.write(json.dumps({"sets": sets, "sites": sites}, separators=(",", ":")).encode())
    print("sites", sum(len(v) for v in sites.values()), "sets", len(sets), "unstable", sorted(unstable), "bytes", Path(out).stat().st_size)

if __name__ == "__main__": main()
