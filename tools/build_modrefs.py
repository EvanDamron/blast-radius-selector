"""Build map/modrefs.json.gz from the pinned commit: for every module-level name of a
sqlglot module, the tests that execute a reference to it.

A reference is any Name load, any attribute load `x.NAME`, or any `from ... import NAME`
anywhere in sqlglot/ or tests/ (the module is not resolved: a same-named identifier
elsewhere counts too). A reference inside a function body counts for the tests that
executed its line; a reference executed at import counts for every test that imported
the file. Names that also occur as string constants get no entry (they may be looked up
by name), and neither do names of modules that inspect their own namespace.
usage: build_modrefs.py SRC COV.json.gz OUT.json.gz"""
import ast, gzip, json, re, sys
from pathlib import Path

def main():
    src, covp, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    raw = json.loads(gzip.open(covp).read())
    sets = raw["sets"]; cache = {}
    def bits(i):
        if i not in cache: cache[i] = int(sets[i], 16)
        return cache[i]
    def at_lines(path, lo, hi):
        # function code only: a reference inside a def or lambda runs when that code runs
        b = 0
        for m in (raw["lines"].get(path),):
            if m:
                for ln in range(lo, hi + 1):
                    i = m.get(str(ln))
                    if i is not None: b |= bits(i)
        return b
    imp_cache = {}
    def importers(path):
        if path not in imp_cache:
            b = 0
            for m in (raw["lines"].get(path, {}), raw["body"].get(path, {})):
                for i in set(m.values()): b |= bits(i)
            imp_cache[path] = b
        return imp_cache[path]

    files = sorted((src / "sqlglot").rglob("*.py")) + sorted((src / "tests").rglob("*.py"))
    refs = {}      # name -> bitset of tests executing a reference
    strings = set(); idents = set()
    trees = {}
    for p in files:
        rel = str(p.relative_to(src)); tree = ast.parse(p.read_text()); trees[rel] = tree
        def add(name, node, deferred):
            idents.add(name)
            b = at_lines(rel, node.lineno, getattr(node, "end_lineno", node.lineno)) if deferred else importers(rel)
            refs[name] = refs.get(name, 0) | b
        def walk(node, deferred, typing_only):
            for child in ast.iter_child_nodes(node):
                d = deferred
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # the body runs when called; decorators and defaults run at definition
                    d = deferred or child in node.body
                    if child is node.returns or isinstance(child, ast.arguments):
                        pass
                elif isinstance(node, ast.Lambda):
                    d = deferred or child is node.body
                t_only = typing_only
                if isinstance(node, ast.If) and child in node.body and "TYPE_CHECKING" in ast.dump(node.test):
                    t_only = True
                if isinstance(child, ast.Name):
                    if isinstance(child.ctx, ast.Load) and not t_only: add(child.id, child, d)
                    else: idents.add(child.id)
                elif isinstance(child, ast.Attribute):
                    if isinstance(child.ctx, ast.Load) and not t_only: add(child.attr, child, d)
                    else: idents.add(child.attr)
                elif isinstance(child, ast.ImportFrom):
                    for a in child.names:
                        if a.name == "*":
                            # star import: every public name of the source may be used here by bare name
                            star.add(rel)
                        elif not t_only:
                            add(a.name, child, d)
                elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if child.value.isidentifier(): strings.add(child.value)
                    else:
                        for part in child.value.replace(".", " ").replace(",", " ").split():
                            if part.isidentifier(): strings.add(part)
                elif isinstance(child, ast.arg):
                    idents.add(child.arg)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    idents.add(child.name)
                walk(child, d, t_only)
        star = set()
        walk(tree, False, False)
    # annotations are not evaluated under `from __future__ import annotations`, but counting them is only conservative.
    entries = {}; reflective = []
    for rel, tree in trees.items():
        if not rel.startswith("sqlglot/"): continue
        text = (src / rel).read_text()
        # A module that inspects its own namespace generically (not by a spelled-out name).
        # dir()/getmembers() on other objects only pick out classes, which get no entry anyway.
        refl = bool(re.search(r"\bglobals\(|\bvars\(\)|\blocals\(|__all__|import \*|\bdir\(\)", text))
        uses_subclasses = "subclasses(" in text
        if refl: reflective.append(rel)
        names = {}
        for st in tree.body:
            kind = None; bound = []
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)): kind, bound = "def", [st.name]
            elif isinstance(st, ast.ClassDef): kind, bound = "class", [st.name]
            elif isinstance(st, ast.Assign): kind, bound = "assign", [t.id for t in st.targets if isinstance(t, ast.Name)]
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None: kind, bound = "assign", [st.target.id]
            elif isinstance(st, (ast.Import, ast.ImportFrom)): kind, bound = "import", [(a.asname or a.name).split(".")[0] for a in st.names if a.name != "*"]
            for n in bound:
                names.setdefault(n, []).append(kind)
        for n, kinds in names.items():
            if refl or n in strings or n.startswith("__") or len(kinds) != 1 or kinds[0] == "class":
                continue
            entries.setdefault(rel, {})[n] = refs.get(n, 0)
    s = []; index = {}
    def idx(b):
        if not b: return -1
        if b not in index: index[b] = len(s); s.append(format(b, "x"))
        return index[b]
    data = {"sets": s, "names": {rel: {n: idx(b) for n, b in d.items()} for rel, d in entries.items()},
            "idents": sorted(idents | strings)}
    with gzip.open(out, "wb", compresslevel=9) as fh:
        fh.write(json.dumps(data, separators=(",", ":")).encode())
    print("modules", len(entries), "names", sum(len(d) for d in entries.values()), "sets", len(s), "reflective modules", len(reflective), "bytes", Path(out).stat().st_size)
    print("reflective:", reflective[:20])

if __name__ == "__main__":
    main()
