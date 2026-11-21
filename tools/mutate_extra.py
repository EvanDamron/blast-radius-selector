"""Extra mutation kinds for tools/mutate.py: string literals and swapped statements."""
import ast


def string_literal(rng, files, pick, rel, parses_differently):
    for _ in range(400):
        p, text, tree, lines = pick(rng, files)
        cands = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and 1 < len(n.value) < 40 and n.lineno == n.end_lineno and n.value.strip()]
        if not cands:
            continue
        n = rng.choice(cands)
        line = lines[n.lineno - 1]
        seg = line[n.col_offset:n.end_col_offset]
        if len(seg) < 3 or seg[0] not in "'\"" or seg[:3] in ('"' * 3, "'" * 3) or seg[-1] != seg[0]:
            continue
        body = seg[1:-1]
        new_body = body.lower() if body != body.lower() else body.upper()
        if new_body == body:
            new_body = body + "_"
        lines[n.lineno - 1] = line[:n.col_offset] + seg[0] + new_body + seg[0] + line[n.end_col_offset:]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no string literal found")


def swap_statements(rng, files, pick, rel, parses_differently):
    for _ in range(400):
        p, text, tree, lines = pick(rng, files)
        pairs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a, b in zip(node.body, node.body[1:]):
                    if (a.lineno == a.end_lineno and b.lineno == b.end_lineno and b.lineno == a.lineno + 1
                            and not isinstance(a, ast.Return) and not isinstance(b, ast.Return)):
                        pairs.append((a.lineno, b.lineno))
        if not pairs:
            continue
        x, y = rng.choice(pairs)
        lines[x - 1], lines[y - 1] = lines[y - 1], lines[x - 1]
        new = "\n".join(lines)
        if parses_differently(text, new):
            return rel(p), text, new
    raise RuntimeError("no swappable statements found")
