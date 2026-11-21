#!/usr/bin/env python3
"""Generate synthetic patches against pinned sqlglot for local evaluation.

usage: mutate.py OUT_DIR COUNT [--seed N] [--prefix s]

Kinds: code (one-token mutation in sqlglot/), comment (a comment line added),
fixture (a token changed in a tests/fixtures file), multi (2-3 of those), and
extra kinds the sample does not show: statement deletion, test-file edits,
docstring edits, class-level constant edits, non-Python files.
The labels come from running the suite (tools/label.py), never from here.
"""
import argparse
import os
import ast
import difflib
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "work" / "sqlglot"

COMMENTS = [
    "# Cheap check first", "# Kept for backwards compatibility", "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation", "# Keep in sync with the generator",
    "# TODO: revisit", "# NOTE: order matters here", "# See the docs for details",
]

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" is not ", " is "), (r" is None", " == None"), (r" is ", " == "),
    (r" and ", " or "), (r" or ", " and "), (r"\bnot ", ""), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r"if ", "if not "), (r" in ", " not in "), (r"\+ 1\b", "- 1"),
    (r"- 1\b", "+ 1"), (r"\breturn None\b", "return 0"),
]


def py_files(sub):
    return sorted(p for p in (SRC / sub).rglob("*.py") if p.stat().st_size > 300)


def rel(p):
    return str(p.relative_to(SRC))


def make_diff(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


def parses_differently(old, new):
    try:
        t2 = ast.parse(new)
    except SyntaxError:
        return False
    return ast.dump(ast.parse(old)) != ast.dump(t2)


def code_mutation(rng, files):
    for _ in range(200):
        p = rng.choice(files)
        text = p.read_text()
        lines = text.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(("import ", "from ", '"""', "'")):
            continue
        ops = [(pat, rep) for pat, rep in SWAPS if re.search(pat, line)]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
        choice = rng.random()
        if nums and (choice < 0.25 or not ops):
            m = rng.choice(nums)
            v = int(m.group(1))
            nv = v + rng.choice([1, -1]) if v > 0 else 1
            new_line = line[: m.start()] + str(nv) + line[m.end():]
        elif ops:
            pat, rep = rng.choice(ops)
            ms = list(re.finditer(pat, line))
            m = rng.choice(ms)
            new_line = line[: m.start()] + re.sub(pat, rep, m.group(0)) + line[m.end():]
        else:
            continue
        lines[i] = new_line
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no mutation found")


def comment_mutation(rng, files):
    for _ in range(200):
        p = rng.choice(files)
        text = p.read_text()
        lines = text.split("\n")
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        indent = re.match(r"\s*", nxt).group(0)
        new = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return rel(p), text, new
    raise RuntimeError


def fixture_mutation(rng):
    fx = sorted(q for q in (SRC / "tests" / "fixtures").rglob("*") if q.is_file() and q.suffix in (".sql", ".json", ".csv"))
    for _ in range(200):
        p = rng.choice(fx)
        text = p.read_text()
        lines = text.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.startswith("--"):
            continue
        words = list(re.finditer(r"\S+", line))
        if p.suffix != ".sql" and rng.random() < 0.5:
            continue
        m = rng.choice(words)
        r = rng.random()
        if r < 0.3:
            new_line = line[: m.end()] + " 42" + line[m.end():]
        elif r < 0.5:
            new_line = line[: m.start()] + "0 " + line[m.start():]
        elif r < 0.7:
            new_line = line[: m.start()] + line[m.end():]
        else:
            new_line = line[: m.start()] + m.group(0)[::-1] + line[m.end():]
        if new_line == line:
            continue
        lines[i] = new_line
        return rel(p), text, "\n".join(lines)
    raise RuntimeError


def delete_statement(rng, files):
    for _ in range(300):
        p = rng.choice(files)
        text = p.read_text()
        tree = ast.parse(text)
        stmts = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr, ast.Return, ast.AugAssign, ast.If))
                 and n.lineno == n.end_lineno]
        if not stmts:
            continue
        n = rng.choice(stmts)
        lines = text.split("\n")
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        new_lines = lines[: n.lineno - 1] + [indent + "pass"] + lines[n.lineno:]
        new = "\n".join(new_lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def test_file_edit(rng):
    return code_mutation(rng, py_files("tests"))


def docstring_edit(rng, files):
    for _ in range(300):
        p = rng.choice(files)
        text = p.read_text()
        lines = text.split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip().startswith('"""') and len(l.strip()) > 8]
        if not idx:
            continue
        i = rng.choice(idx)
        lines[i] = lines[i].replace('"""', '"""Note: ', 1)
        new = "\n".join(lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return rel(p), text, new
    raise RuntimeError


def class_constant(rng, files):
    for _ in range(400):
        p = rng.choice(files)
        text = p.read_text()
        tree = ast.parse(text)
        cands = []
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef):
                for st in cls.body:
                    if isinstance(st, ast.Assign) and st.lineno == st.end_lineno and isinstance(st.value, ast.Constant):
                        cands.append(st)
        if not cands:
            continue
        st = rng.choice(cands)
        lines = text.split("\n")
        line = lines[st.lineno - 1]
        v = st.value.value
        if isinstance(v, bool):
            nl = line.replace(str(v), str(not v))
        elif isinstance(v, int):
            nl = re.sub(r"=\s*-?\d+", f"= {v + 1}", line, count=1)
        elif isinstance(v, str) and v:
            nl = line.replace(repr(v)[1:-1], repr(v)[1:-1] + "x", 1)
        elif v is None:
            nl = line.replace("None", "1")
        else:
            continue
        if nl == line:
            continue
        lines[st.lineno - 1] = nl
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def _pick_tree(rng, files):
    p = rng.choice(files)
    text = p.read_text()
    return p, text, ast.parse(text), text.split("\n")


def rename_def(rng, files):
    for _ in range(300):
        p, text, tree, lines = _pick_tree(rng, files)
        defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and not n.name.startswith("__")]
        if not defs:
            continue
        n = rng.choice(defs)
        i = n.lineno - 1 + len(n.decorator_list)
        while i < len(lines) and f"def {n.name}" not in lines[i]:
            i += 1
        if i >= len(lines):
            continue
        lines[i] = lines[i].replace(f"def {n.name}", f"def {n.name}_old", 1)
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def dict_entry_delete(rng, files):
    for _ in range(400):
        p, text, tree, lines = _pick_tree(rng, files)
        cands = []
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef):
                for st in cls.body:
                    if isinstance(st, ast.Assign) and isinstance(st.value, (ast.Dict, ast.Set)):
                        elts = st.value.values if isinstance(st.value, ast.Dict) else st.value.elts
                        for k, e in enumerate(elts):
                            key = st.value.keys[k] if isinstance(st.value, ast.Dict) else e
                            if key is not None and key.lineno == e.end_lineno and key.lineno != st.lineno:
                                cands.append(key.lineno)
        if not cands:
            continue
        ln = rng.choice(cands)
        new = "\n".join(lines[: ln - 1] + lines[ln:])
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def default_arg(rng, files):
    for _ in range(400):
        p, text, tree, lines = _pick_tree(rng, files)
        cands = []
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef):
                for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                    if isinstance(d, ast.Constant) and isinstance(d.value, (bool, int)) and d.lineno == d.end_lineno:
                        cands.append(d)
        if not cands:
            continue
        d = rng.choice(cands)
        line = lines[d.lineno - 1]
        old = line[d.col_offset:d.end_col_offset]
        v = d.value
        rep = str(not v) if isinstance(v, bool) else str(v + 1)
        lines[d.lineno - 1] = line[:d.col_offset] + rep + line[d.end_col_offset:]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def return_none(rng, files):
    for _ in range(400):
        p, text, tree, lines = _pick_tree(rng, files)
        rets = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value is not None
                and n.lineno == n.end_lineno and not (isinstance(n.value, ast.Constant) and n.value.value is None)]
        if not rets:
            continue
        n = rng.choice(rets)
        line = lines[n.lineno - 1]
        indent = re.match(r"\s*", line).group(0)
        lines[n.lineno - 1] = indent + "return None"
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def add_override(rng, files):
    """Add a method to a class that overrides a base method by delegating oddly."""
    for _ in range(400):
        p, text, tree, lines = _pick_tree(rng, files)
        classes = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.bases and c.body]
        if not classes:
            continue
        c = rng.choice(classes)
        methods = [m for m in c.body if isinstance(m, ast.FunctionDef) and m.args.args and m.args.args[0].arg == "self"]
        if not methods:
            continue
        m = rng.choice(methods)
        indent = re.match(r"\s*", lines[m.lineno - 1 - len(m.decorator_list)]).group(0)
        # A wrapper that swallows the result for one specific case.
        body = [
            "",
            f"{indent}def {m.name}_wrapped(self, *args, **kwargs):",
            f"{indent}    return self.{m.name}(*args, **kwargs)",
        ]
        ins = c.body[-1].end_lineno
        new = "\n".join(lines[:ins] + body + lines[ins:])
        if parses_differently(text, new):
            # a pure addition nobody calls: should break nothing
            return rel(p), text, new
    raise RuntimeError


def import_drop(rng, files):
    for _ in range(400):
        p, text, tree, lines = _pick_tree(rng, files)
        imps = [n for n in tree.body if isinstance(n, ast.ImportFrom) and len(n.names) > 1 and n.lineno == n.end_lineno]
        if not imps:
            continue
        n = rng.choice(imps)
        drop = rng.choice(n.names)
        names = [a for a in n.names if a is not drop]
        line = "from " + "." * n.level + (n.module or "") + " import " + ", ".join(
            a.name + (f" as {a.asname}" if a.asname else "") for a in names)
        lines[n.lineno - 1] = line
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError


def other_file(rng):
    p = rng.choice([SRC / "README.md", SRC / "CHANGELOG.md", SRC / "CONTRIBUTING.md", SRC / "setup.py"])
    text = p.read_text()
    lines = text.split("\n")
    i = rng.randrange(len(lines))
    lines.insert(i, "Note: see the docs." if p.suffix == ".md" else "# see the docs")
    return rel(p), text, "\n".join(lines)


def one(rng, kind, src_files):
    if kind == "code":
        return [code_mutation(rng, src_files)]
    if kind == "comment":
        return [comment_mutation(rng, src_files)]
    if kind == "fixture":
        return [fixture_mutation(rng)]
    if kind == "delete":
        return [delete_statement(rng, src_files)]
    if kind == "testfile":
        return [test_file_edit(rng)]
    if kind == "docstring":
        return [docstring_edit(rng, src_files)]
    if kind == "classconst":
        return [class_constant(rng, src_files)]
    if kind == "other":
        return [other_file(rng)]
    simple = {"rename": rename_def, "dictdel": dict_entry_delete, "default": default_arg,
              "retnone": return_none, "override": add_override, "importdrop": import_drop}
    if kind in simple:
        return [simple[kind](rng, src_files)]
    if kind in ("string", "swap"):
        import mutate_extra
        fn = mutate_extra.string_literal if kind == "string" else mutate_extra.swap_statements
        return [fn(rng, src_files, _pick_tree, rel, parses_differently)]
    if kind in ("annotation", "decorator", "base", "kwswap"):
        import mutate_extra2
        fn = {"annotation": mutate_extra2.annotation, "decorator": mutate_extra2.decorator_drop,
              "base": mutate_extra2.base_swap, "kwswap": mutate_extra2.kwarg_swap}[kind]
        return [fn(rng, src_files, _pick_tree, rel, parses_differently)]
    if kind in ("reexport", "helpers"):
        import mutate_extra2
        fn = mutate_extra2.reexport_drop if kind == "reexport" else mutate_extra2.helpers_edit
        return [fn(rng, SRC, rel, parses_differently)]
    if kind == "samefile":
        # two one-token edits in one file
        path, old, mid = code_mutation(rng, src_files)
        for _ in range(50):
            p2, old2, new2 = code_mutation(rng, [SRC / path])
            lines_a, lines_b = mid.split("\n"), new2.split("\n")
            diff = [i for i, (x, y) in enumerate(zip(old2.split("\n"), lines_b)) if x != y]
            if len(diff) == 1 and lines_a[diff[0]] == old2.split("\n")[diff[0]]:
                lines_a[diff[0]] = lines_b[diff[0]]
                new = "\n".join(lines_a)
                if parses_differently(old, new):
                    return [(path, old, new)]
        return [(path, old, mid)]
    if kind == "multi":
        parts = []
        used = set()
        for k in rng.sample(["code", "comment", "comment", "fixture", "classconst"], rng.choice([2, 3])):
            ch = one(rng, k, src_files)[0]
            if ch[0] not in used:
                used.add(ch[0])
                parts.append(ch)
        return parts
    raise ValueError(kind)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("count", type=int)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--prefix", default="m")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    src_files = py_files("sqlglot")
    ap_kinds = os.environ.get("MUTATE_KINDS")
    kinds = (["code"] * 40 + ["comment"] * 10 + ["fixture"] * 15 + ["multi"] * 10 + ["delete"] * 8
             + ["testfile"] * 5 + ["docstring"] * 3 + ["classconst"] * 7 + ["other"] * 2)
    if ap_kinds:
        kinds = ap_kinds.split(",")
    meta = {}
    for k in range(a.count):
        kind = rng.choice(kinds)
        changes = one(rng, kind, src_files)
        patch = "".join(make_diff(path, old, new) for path, old, new in sorted(changes))
        cid = f"{a.prefix}{a.seed}x{k:03d}"
        (out / "commits" / f"{cid}.patch").write_text(patch)
        meta[cid] = kind
    import json
    (out / "kinds.json").write_text(json.dumps(meta, indent=0))


if __name__ == "__main__":
    main()
