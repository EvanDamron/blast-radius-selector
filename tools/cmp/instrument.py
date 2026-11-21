"""Write an instrumented copy of the pinned checkout. usage: instrument.py SRC DST
sqlglot/: comparisons, tracked attribute loads, getattr/hasattr calls, class-level
dict literals. tests/: attribute loads and getattr/hasattr calls only.
Also writes DST/_brmeta.json: class-level names, and the tracked names that occur as
string constants anywhere (reads by name that the instrumentation cannot follow)."""
import ast, json, shutil, sys
from pathlib import Path

OPS = [ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot]


def class_level_names(tree, out, prefix=""):
    """{qualname: [names assigned in the class body]} for classes nested in classes."""
    for st in tree.body:
        if isinstance(st, ast.ClassDef):
            q = prefix + st.name
            names = out.setdefault(q, [])
            for s in st.body:
                targets = []
                if isinstance(s, ast.Assign):
                    targets = s.targets
                elif isinstance(s, ast.AnnAssign) and s.value is not None:
                    targets = [s.target]
                for t in targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("__"):
                        names.append(t.id)
            class_level_names(st, out, q + ".")


class T(ast.NodeTransformer):
    def __init__(self, rel, tracked, full):
        self.rel = rel
        self.tracked = tracked
        self.full = full
        self.n = 0
        self.cls = []
        self.func = 0

    def visit_ClassDef(self, node):
        self.cls.append(node.name)
        saved, self.func = self.func, 0
        # decorators / bases are evaluated outside the class body
        node.decorator_list = [self.visit(x) for x in node.decorator_list]
        node.bases = [self.visit(x) for x in node.bases]
        node.keywords = [self.visit(x) for x in node.keywords]
        body = []
        for st in node.body:
            raw = st.value if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
            plain_keys = isinstance(raw, ast.Dict) and all(
                k is None or isinstance(k, (ast.Constant, ast.Name, ast.Attribute)) for k in raw.keys
            )
            st = self.visit(st)
            if self.full and not self.func:
                value = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    value, name = st.value, st.targets[0].id
                elif isinstance(st, ast.AnnAssign) and st.value is not None and isinstance(st.target, ast.Name):
                    value, name = st.value, st.target.id
                if isinstance(value, (ast.Dict, ast.DictComp)):
                    site = f"{self.rel}|{'.'.join(self.cls)}.{name}"
                    args = [ast.Constant(site), value]
                    if isinstance(value, ast.Dict) and plain_keys:
                        args.append(ast.Tuple(elts=[k for k in value.keys if k is not None], ctx=ast.Load()))
                    st.value = ast.Call(func=ast.Name(id="_brrec_d_", ctx=ast.Load()), args=args, keywords=[])
                    self.n += 1
            body.append(st)
        node.body = body
        self.func = saved
        self.cls.pop()
        return node

    def _func(self, node):
        self.func += 1
        self.generic_visit(node)
        self.func -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_Compare(self, node):
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        self.generic_visit(node)
        if not self.full or len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        self.n += 1
        call = ast.Call(
            func=ast.Name(id="_brrec_c_", ctx=ast.Load()),
            args=[ast.Constant(site), ast.Constant(OPS.index(type(node.ops[0]))), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.tracked:
            self.n += 1
            call = ast.Call(func=ast.Name(id="_brrec_a_", ctx=ast.Load()), args=[node.value, ast.Constant(node.attr)], keywords=[])
            return ast.copy_location(call, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr") and not node.keywords:
            if not any(isinstance(x, ast.Starred) for x in node.args):
                node.func = ast.Name(id="_brrec_g_" if node.func.id == "getattr" else "_brrec_h_", ctx=ast.Load())
                self.n += 1
        return node


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests", "benchmarks", "posts", "pdoc", "*.png"))
    files = sorted((dst / "sqlglot").rglob("*.py")) + sorted((dst / "tests").rglob("*.py"))
    trees = {}
    defs = {}
    for p in files:
        rel = str(p.relative_to(dst))
        trees[rel] = ast.parse(p.read_text())
        if rel.startswith("sqlglot/"):
            d = {}
            class_level_names(trees[rel], d)
            defs[rel] = d
    tracked = {n for d in defs.values() for names in d.values() for n in names}
    # Names whose reads the instrumentation cannot follow: used as a string constant
    # (getattr by name, __dict__ lookups, mock.patch.object), or loaded implicitly by an
    # augmented assignment, a delete, or a match pattern.
    strings = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in tracked:
                strings.add(node.value)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                strings.add(node.target.attr)
            elif isinstance(node, ast.Delete):
                strings.update(t.attr for t in node.targets if isinstance(t, ast.Attribute))
            elif isinstance(node, ast.MatchValue):
                strings.update(n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute))
            elif isinstance(node, ast.MatchClass):
                strings.update(node.kwd_attrs)
    total = 0
    for p in files:
        rel = str(p.relative_to(dst))
        tree = trees[rel]
        tr = T(rel, tracked, rel.startswith("sqlglot/"))
        tree = tr.visit(tree)
        if not tr.n:
            continue
        total += tr.n
        i = 0
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            i = 1
        while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
            i += 1
        imp = ast.ImportFrom(
            module="_brrec",
            names=[ast.alias(name=x, asname=f"_brrec_{x}_") for x in "cadgh"],
            level=0,
        )
        body.insert(i, imp)
        ast.fix_missing_locations(tree)
        p.write_text(ast.unparse(tree))
    shutil.copy(Path(__file__).parent / "_brrec.py", dst / "_brrec.py")
    json.dump({"defs": defs, "strings": sorted(strings)}, open(dst / "_brmeta.json", "w"))
    print("instrumented points", total, "tracked names", len(tracked), "as strings", len(strings))


if __name__ == "__main__":
    main()
