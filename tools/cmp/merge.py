"""Merge per-test records into the maps the selector reads.
usage: merge.py RECDIR TESTS_JSON META.json KEYS.json OUTDIR

cmp.json.gz   {"sets": [...], "sites": {path: {"line:col": [exec, d0..d7]}}}
attr.json.gz  {"sets": [...], "attr": {"module:Class.NAME": set}, "classes": [...],
               "strings": [...], "defs": {...}, "special": [...]}
dict.json.gz  {"sets": [...], "sites": {site: {"c","a","k","l": set, "v": {key: set},
               "h": {key: set}}}, "keys": {site: [key of each explicit entry]}}
A set is an index into "sets" (hex bitsets over test numbers); -1 is the empty set.
"""
import gzip, json, sys
from pathlib import Path


class Sets:
    def __init__(self):
        self.sets = []
        self.index = {}

    def idx(self, b):
        if not b:
            return -1
        i = self.index.get(b)
        if i is None:
            i = self.index[b] = len(self.sets)
            self.sets.append(format(b, "x"))
        return i


def write(path, obj):
    with gzip.open(path, "wb", compresslevel=9) as fh:
        fh.write(json.dumps(obj, separators=(",", ":")).encode())
    print(path, Path(path).stat().st_size, "bytes")


def main():
    rec, tj, meta, keys, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], Path(sys.argv[5])
    tests = json.load(open(tj))["tests"]
    meta = json.load(open(meta))
    keys = json.load(open(keys))
    cmp_acc, attr_acc, dict_acc = {}, {}, {}
    missing = []
    recs = [Path(x) for x in str(rec).split(",")]
    diff_tests = 0
    unstable = set()  # comparison sites whose record changed between passes
    for t in tests:
        loaded = []
        for r in recs:
            f = r / f"t{t['number']}.json.gz"
            if f.exists():
                loaded.append(json.load(gzip.open(f, "rt")))
            elif (r / f"t{t['number']}.json").exists():
                loaded.append(json.load(open(r / f"t{t['number']}.json")))
        if len(loaded) != len(recs):
            missing.append(t["number"])
            continue
        d = {}
        for x in loaded:
            for k, mask in x.items():
                d[k] = d.get(k, 0) | mask
        if any(x != d for x in loaded):
            diff_tests += 1
            for x in loaded:
                for k, mask in d.items():
                    if x.get(k, 0) != mask and "|" not in k:
                        unstable.add(k)
        bit = 1 << t["number"]
        for k, mask in d.items():
            if k.startswith("A|"):
                attr_acc[k[2:]] = attr_acc.get(k[2:], 0) | bit
            elif k.startswith("D|"):
                dict_acc[k[2:]] = dict_acc.get(k[2:], 0) | bit
            else:
                a = cmp_acc.get(k)
                if a is None:
                    a = cmp_acc[k] = [0] * 9
                i = 0
                while mask:
                    if mask & 1:
                        a[i] |= bit
                    mask >>= 1
                    i += 1
    print("tests whose records differ between passes:", diff_tests, "unstable comparison sites:", sorted(unstable))
    # A site whose outcomes depend on the run (it compares hash values, say) gets no
    # narrowing: every operator counts as differing wherever the site executed.
    for site in unstable:
        a = cmp_acc[site]
        for i in range(1, 9):
            a[i] = a[0]
    if missing:
        raise SystemExit(f"missing records for {len(missing)} tests: {missing[:10]}")
    s = Sets()
    sites = {}
    for site, a in sorted(cmp_acc.items()):
        path, line, col = site.rsplit(":", 2)
        sites.setdefault(path, {})[f"{line}:{col}"] = [s.idx(b) for b in a]
    write(out / "cmp.json.gz", {"n": len(tests), "sets": s.sets, "sites": sites})
    if attr_acc:
        s = Sets()
        attr = {k: s.idx(b) for k, b in sorted(attr_acc.items())}
        classes = sorted({k.rsplit(".", 1)[0] for k in attr})
        write(out / "attr.json.gz", {"sets": s.sets, "attr": attr, "classes": classes, "strings": meta["strings"],
                                     "defs": meta["defs"], "special": keys["special"]})
    if dict_acc:
        s = Sets()
        dsites = {}
        for k, b in sorted(dict_acc.items()):
            parts = k.split("|", 3)
            site = parts[0] + "|" + parts[1]
            e = dsites.setdefault(site, {"c": -1, "a": -1, "k": -1, "l": -1, "v": {}, "h": {}})
            kind = parts[2]
            if kind in ("v", "h"):
                e[kind][parts[3]] = s.idx(b)
            else:
                e[kind] = s.idx(b)
        write(out / "dict.json.gz", {"sets": s.sets, "sites": dsites, "keys": keys["keys"]})
    print("cmp sites", len(cmp_acc), "attr keys", len(attr_acc), "dict keys", len(dict_acc))


if __name__ == "__main__":
    main()
