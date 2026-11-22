"""Instrumented copy of the pinned checkout for operand-relation recording: every
single-operator comparison in sqlglot/ becomes _brrel.c(site, op, left, right).
usage: instrument_rel.py SRC DST"""
import ast, shutil, sys
from pathlib import Path
OPS = [ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot]

class T(ast.NodeTransformer):
    def __init__(self, rel): self.rel = rel; self.n = 0
    def visit_Compare(self, node):
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS: return node
        self.n += 1
        return ast.copy_location(ast.Call(func=ast.Name(id="_brrel_c_", ctx=ast.Load()),
            args=[ast.Constant(site), ast.Constant(OPS.index(type(node.ops[0]))), node.left, node.comparators[0]], keywords=[]), node)

def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests", "benchmarks", "posts", "pdoc", "*.png"))
    total = 0
    for p in sorted((dst / "sqlglot").rglob("*.py")):
        tree = ast.parse(p.read_text()); tr = T(str(p.relative_to(dst))); tree = tr.visit(tree)
        if not tr.n: continue
        total += tr.n
        body = tree.body; i = 0
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str): i = 1
        while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__": i += 1
        body.insert(i, ast.ImportFrom(module="_brrel", names=[ast.alias(name="c", asname="_brrel_c_")], level=0))
        ast.fix_missing_locations(tree); p.write_text(ast.unparse(tree))
    shutil.copy(Path(__file__).parent / "_brrel.py", dst / "_brrel.py")
    print("comparison sites", total)

if __name__ == "__main__": main()
