"""More synthetic edit kinds (for robustness checks): annotations, decorators, base
classes, two edits in one file, test helpers, package re-exports, keyword swaps."""
import ast
import re


def _lines_of(text):
    return text.split("\n")


def annotation(rng, files, pick, rel, parses_differently):
    """Change one parameter or return annotation."""
    for _ in range(200):
        p, text, tree, _ = pick(rng, files)
        anns = [n.returns for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None]
        anns += [a.annotation for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args if a.annotation is not None]
        anns = [a for a in anns if a.lineno == a.end_lineno]
        if not anns:
            continue
        a = rng.choice(anns)
        lines = _lines_of(text)
        line = lines[a.lineno - 1]
        repl = rng.choice(["int", "str", "t.Any", "bool"])
        lines[a.lineno - 1] = line[: a.col_offset] + repl + line[a.end_col_offset:]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no annotation")


def decorator_drop(rng, files, pick, rel, parses_differently):
    for _ in range(200):
        p, text, tree, _ = pick(rng, files)
        decs = [d for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for d in n.decorator_list]
        if not decs:
            continue
        d = rng.choice(decs)
        lines = _lines_of(text)
        if d.lineno != d.end_lineno or not lines[d.lineno - 1].strip().startswith("@"):
            continue
        del lines[d.lineno - 1]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no decorator")


def base_swap(rng, files, pick, rel, parses_differently):
    """Replace a class's first base with another class defined in the same module."""
    for _ in range(200):
        p, text, tree, _ = pick(rng, files)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        with_base = [c for c in classes if c.bases and isinstance(c.bases[0], ast.Name)]
        if len(classes) < 2 or not with_base:
            continue
        c = rng.choice(with_base)
        others = [o.name for o in classes if o.lineno < c.lineno and o.name not in (c.name, c.bases[0].id)]
        if not others:
            continue
        b = c.bases[0]
        lines = _lines_of(text)
        line = lines[b.lineno - 1]
        lines[b.lineno - 1] = line[: b.col_offset] + rng.choice(others) + line[b.end_col_offset:]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no base swap")


def kwarg_swap(rng, files, pick, rel, parses_differently):
    for _ in range(300):
        p, text, tree, _ = pick(rng, files)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and len(n.keywords) >= 2
                 and all(k.arg for k in n.keywords) and n.lineno == n.end_lineno]
        if not calls:
            continue
        call = rng.choice(calls)
        k1, k2 = rng.sample(call.keywords, 2)
        if k1.value.lineno != k2.value.lineno:
            continue
        lines = _lines_of(text)
        line = lines[call.lineno - 1]
        a, b = sorted([k1.value, k2.value], key=lambda v: v.col_offset)
        sa, sb = line[a.col_offset:a.end_col_offset], line[b.col_offset:b.end_col_offset]
        line = line[: a.col_offset] + sb + line[a.end_col_offset: b.col_offset] + sa + line[b.end_col_offset:]
        lines[call.lineno - 1] = line
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no kwarg swap")


def reexport_drop(rng, src, rel, parses_differently):
    """Drop one name from a `from x import (a, b, ...)` in a package __init__."""
    inits = [p for p in sorted((src / "sqlglot").rglob("__init__.py")) if p.stat().st_size > 200]
    for _ in range(200):
        p = rng.choice(inits)
        text = p.read_text()
        lines = _lines_of(text)
        idx = [i for i, l in enumerate(lines) if re.match(r"\s+\w+,\s*$", l)]
        if not idx:
            continue
        del lines[rng.choice(idx)]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no reexport")


def helpers_edit(rng, src, rel, parses_differently):
    p = src / "tests" / "helpers.py"
    text = p.read_text()
    lines = _lines_of(text)
    for _ in range(200):
        i = rng.randrange(len(lines))
        line = lines[i]
        m = re.search(r'"(bigint|double|varchar|int|string|date|decimal)"', line)
        if m:
            lines[i] = line[: m.start()] + '"' + rng.choice(["bigint", "double", "varchar", "date"]) + '"' + line[m.end():]
        elif re.search(r"\.strip\(\)", line):
            lines[i] = line.replace(".strip()", "", 1)
        else:
            continue
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no helpers edit")
