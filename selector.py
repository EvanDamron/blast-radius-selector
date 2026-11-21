"""Blast Radius test selector.

For each patch in /data/commits, work out which sqlglot tests could observe the
change and split them across workers. The evidence comes from maps built once
from the pinned sqlglot commit (map/cov.json.gz): which test executes which
line, and which test opens which file.

A changed Python file is compared with its original as an AST, so formatting
and comment edits that change no code select nothing. A change inside a
function selects the tests that ran that function. A change at module or class
level selects every test that imported the module. A syntax error selects every
importer. Any other file selects the tests that opened it.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", str(Path(__file__).resolve().parent / "map" / "cov.json.gz")))

WORKER_COST = 2000  # milliseconds
MAX_WORKERS = 8

# Files pytest reads at startup: a broken or pytest-related edit reaches every test.
PYTEST_CONFIG = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"}


def log(*args) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- map


class CoverageMap:
    def __init__(self, path: Path):
        with gzip.open(path, "rb") as fh:
            raw = json.loads(fh.read())
        self.n = raw["n"]
        self._sets_hex = raw["sets"]
        self._sets: dict[int, int] = {}
        self.lines = raw["lines"]
        self.body = raw["body"]
        self.opened = raw["opened"]
        self.classes = raw.get("classes", {})
        self._file_union: dict[str, int] = {}

    def bits(self, index: int) -> int:
        b = self._sets.get(index)
        if b is None:
            b = self._sets[index] = int(self._sets_hex[index], 16)
        return b

    def func_lines(self, path: str, first: int, last: int, include_body: bool = False) -> int:
        """Tests that ran function code on any line in [first, last].

        include_body also counts class bodies nested in the function, whose code
        is recorded with module-level code.
        """
        out = 0
        maps = [self.lines.get(path) or {}]
        if include_body:
            maps.append(self.body.get(path) or {})
        for m in maps:
            for ln in range(first, last + 1):
                i = m.get(str(ln))
                if i is not None:
                    out |= self.bits(i)
        return out

    def at_line(self, path: str, line: int) -> int:
        out = 0
        for m in (self.lines.get(path), self.body.get(path)):
            if m:
                i = m.get(str(line))
                if i is not None:
                    out |= self.bits(i)
        return out

    def class_users(self, key: str) -> int:
        i = self.classes.get(key)
        return self.bits(i) if i is not None else 0

    def importers(self, path: str) -> int:
        """Tests that executed anything in the file: its module body runs on import."""
        u = self._file_union.get(path)
        if u is None:
            u = 0
            for m in (self.lines.get(path, {}), self.body.get(path, {})):
                for i in set(m.values()):
                    u |= self.bits(i)
            self._file_union[path] = u
        return u

    def openers(self, path: str) -> int:
        i = self.opened.get(path)
        return self.bits(i) if i is not None else 0


# ------------------------------------------------------------------------- diffs


class FilePatch:
    def __init__(self):
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list[tuple[int, int, list[str]]] = []  # (old_start, old_len, lines)
        self.binary = False


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git (\S+) (\S+)$", line)
            if m:
                cur.old_path, cur.new_path = _strip(m.group(1)), _strip(m.group(2))
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            i += 1
        elif cur is not None and line.startswith("new file mode"):
            cur.old_path = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new_path = None
        elif cur is not None and line.startswith("rename from "):
            cur.old_path = line[len("rename from "):].strip()
        elif cur is not None and line.startswith("rename to "):
            cur.new_path = line[len("rename to "):].strip()
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        elif cur is not None and (m := HUNK.match(line)):
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body: list[str] = []
            seen_old = seen_new = 0
            i += 1
            while i < len(lines) and (seen_old < old_len or seen_new < new_len):
                h = lines[i]
                if h.startswith("\\"):
                    i += 1
                    continue
                tag = h[:1] if h else " "
                if tag == " ":
                    seen_old += 1
                    seen_new += 1
                elif tag == "-":
                    seen_old += 1
                elif tag == "+":
                    seen_new += 1
                else:
                    break
                body.append(h if h else " ")
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, old_len, body))
            continue
        i += 1
    return files


def apply_hunks(old: str, hunks) -> tuple[str, list[int], list[int], list[int]]:
    """Return (new text, old lines removed, new lines added, insertion anchors).

    An anchor p marks a run of added lines with no removed line beside it,
    inserted between old lines p and p + 1.
    """
    old_lines = old.split("\n")
    out: list[str] = []
    removed: list[int] = []
    added: list[int] = []
    anchors: list[int] = []
    pos = 0  # old lines consumed
    for old_start, old_len, body in hunks:
        tags = [h[0] for h in body]
        for k, tag in enumerate(tags):
            if tag == "+" and (k == 0 or tags[k - 1] != "+"):
                end = k
                while end < len(tags) and tags[end] == "+":
                    end += 1
                if (k == 0 or tags[k - 1] != "-") and (end >= len(tags) or tags[end] != "-"):
                    anchors.append(-1 - k)  # resolved below to an old line number
        start = max(old_start - 1 if old_len > 0 else old_start, pos)
        out.extend(old_lines[pos:start])
        pos = start
        for h in body:
            tag, text = h[0], h[1:]
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else text)
                pos += 1
            elif tag == "-":
                removed.append(pos + 1)
                pos += 1
            else:
                out.append(text)
                added.append(len(out))
        # Resolve anchors of this hunk: old lines consumed before body index k.
        consumed = old_start - 1 if old_len > 0 else old_start
        resolved = []
        for k, tag in enumerate(tags):
            if -1 - k in anchors:
                resolved.append(consumed)
            if tag in " -":
                consumed += 1
        anchors = [a for a in anchors if a >= 0] + resolved
    out.extend(old_lines[pos:])
    return "\n".join(out), removed, added, anchors


# ---------------------------------------------------------------------- analysis

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def sdump(node) -> str:
    """AST dump without positions or ctx, with nested function/lambda bodies elided."""
    if isinstance(node, list):
        return "[" + ",".join(sdump(x) for x in node) + "]"
    if isinstance(node, ast.AST):
        parts = [type(node).__name__]
        lam = isinstance(node, ast.Lambda)
        fn = isinstance(node, _FUNC)
        for name, val in ast.iter_fields(node):
            if name == "ctx":
                continue
            if (fn or lam) and name == "body":
                parts.append("...")
                continue
            parts.append(sdump(val))
        return "(" + " ".join(parts) + ")"
    return repr(node)


class Scope:
    __slots__ = ("qual", "node", "first", "last", "kind")

    def __init__(self, qual, node, first, last, kind):
        self.qual, self.node, self.first, self.last, self.kind = qual, node, first, last, kind

    def own(self) -> str:
        if self.kind == "lambda":
            return sdump(self.node.body)
        return sdump(self.node.body)


def scopes(tree: ast.Module) -> list[Scope]:
    """Every function and lambda, named by its position among classes and functions."""
    out: list[Scope] = []
    counters: dict[str, int] = {}

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNC):
                q = f"{qual}.{child.name}"
                k = counters.get(q, 0)
                counters[q] = k + 1
                q = f"{q}#{k}"
                out.append(Scope(q, child, child.body[0].lineno, child.end_lineno, "def"))
                visit(child, q)
            elif isinstance(child, ast.Lambda):
                q = f"{qual}.<lambda>"
                k = counters.get(q, 0)
                counters[q] = k + 1
                q = f"{q}#{k}"
                out.append(Scope(q, child, child.body.lineno, child.body.end_lineno, "lambda"))
                visit(child, q)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{qual}.{child.name}")
            else:
                visit(child, qual)

    visit(tree, "")
    return out


def innermost(scs: list[Scope], line: int) -> Scope | None:
    best = None
    for sc in scs:
        if sc.first <= line <= sc.last and (best is None or sc.last - sc.first < best.last - best.first):
            best = sc
    return best


def skeleton(tree: ast.Module) -> list[tuple[str, str, ast.AST]]:
    """Module- and class-level statements as (class path, dump, node).

    Function bodies are elided and classes are opened up, so a statement inside
    a class body is its own entry.
    """
    out: list[tuple[str, str, ast.AST]] = []

    def visit(stmts, prefix):
        for st in stmts:
            if isinstance(st, ast.ClassDef):
                out.append((prefix, "class " + sdump([st.name, st.bases, st.keywords, st.decorator_list]), st))
                visit(st.body, prefix + st.name + ".")
            elif _is_docstring(st):
                continue
            else:
                out.append((prefix, sdump(st), st))

    visit(tree.body, "")
    return out


def _is_docstring(st) -> bool:
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


# ------------------------------------------------------------ module-level detail


class Static:
    """map/static.json.gz: function definitions by name and the expression class tree."""

    def __init__(self, path: Path):
        with gzip.open(path, "rb") as fh:
            raw = json.loads(fh.read())
        self.defs: dict[str, list] = raw["defs"]
        self.expr: dict[str, list] = raw["expr"]
        self.expr_by_name: dict[str, list] = raw["expr_by_name"]
        self.expr_by_key: dict[str, list] = raw["expr_by_key"]


_static: Static | None = None
_sources: dict[str, str] | None = None


def static() -> Static:
    global _static
    if _static is None:
        _static = Static(MAP.parent / "static.json.gz")
    return _static


def sources() -> dict[str, str]:
    global _sources
    if _sources is None:
        _sources = {}
        for sub in ("sqlglot", "tests"):
            for p in sorted((SRC / sub).rglob("*.py")):
                _sources[str(p.relative_to(SRC))] = p.read_text(encoding="utf-8", errors="replace")
    return _sources


def readers(names: set[str], cov: CoverageMap, exclude: tuple[str, int, int], tests_only: bool = False) -> int:
    """Tests that ran any line mentioning one of the names (as code or in a string).

    tests_only limits the search to test code: tests that inspect a table directly.
    """
    names = {n for n in names if n}
    if not names:
        return 0
    pat = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b")
    bits = 0
    ex_path, ex_first, ex_last = exclude
    for rel, text in sources().items():
        if tests_only and not rel.startswith("tests/"):
            continue
        if not pat.search(text):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if pat.search(line) and not (rel == ex_path and ex_first <= i <= ex_last):
                bits |= cov.at_line(rel, i)
    return bits


def _targets(st) -> set[str]:
    names: set[str] = set()
    tgts = st.targets if isinstance(st, ast.Assign) else [getattr(st, "target", None)]
    for t in tgts:
        if t is None:
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    return names


def _expr_refs(node) -> set[str]:
    """Expression classes a node names, as exp.Foo or a bare Foo."""
    st = static()
    out: set[str] = set()
    for n in ast.walk(node):
        name = None
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ("exp", "expressions"):
            name = n.attr
        elif isinstance(n, ast.Name):
            name = n.id
        if name and name in st.expr_by_name:
            out.update(st.expr_by_name[name])
    return out


def _func_refs(node) -> set[str]:
    """Names of sqlglot functions a node calls or names directly (not via attributes)."""
    st = static()
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in st.defs and n.id not in st.expr_by_name:
            out.add(n.id)
    return out


def instantiators(keys: set[str], cov: CoverageMap) -> int:
    st = static()
    bits = 0
    for k in keys:
        for d in st.expr.get(k, [None, [k]])[1]:
            bits |= cov.class_users(d)
    return bits


def def_bodies(name: str, cov: CoverageMap) -> int:
    bits = 0
    for rel, first, last in static().defs.get(name, []):
        bits |= cov.func_lines(rel, first, last, include_body=True)
    return bits


def value_signals(nodes, cov: CoverageMap) -> tuple[int, bool]:
    """Tests that exercise what the nodes build: expression classes and sqlglot helpers."""
    bits = 0
    found = False
    for node in nodes:
        if node is None:
            continue
        refs = _expr_refs(node)
        if refs:
            bits |= instantiators(refs, cov)
            found = True
        for name in _func_refs(node):
            bits |= def_bodies(name, cov)
            found = True
        for lam in ast.walk(node):
            if isinstance(lam, ast.Lambda):
                found = True  # a lambda's own lines are compared and selected as a scope
    return bits, found


def _dict_entries(node):
    out = []
    for k, v in zip(node.keys, node.values):
        out.append((sdump(k) if k is not None else "**" + sdump(v), k, v))
    return out


def _expr_class_of(path: str, prefix: str) -> set[str]:
    """Expression class keys for a class path in a module, e.g. 'TriggerProperties.'."""
    if not prefix:
        return set()
    name = prefix.rstrip(".").split(".")[-1]
    keys = set()
    for k in static().expr_by_name.get(name, []):
        if static().expr[k][0] == path:
            keys.add(k)
    return keys


def module_level(path: str, old_tree, new_tree, cov: CoverageMap) -> tuple[int, list[str]]:
    import difflib

    a = skeleton(old_tree)
    b = skeleton(new_tree)
    if [x[:2] for x in a] == [x[:2] for x in b]:
        return 0, []
    imp = cov.importers(path)
    sm = difflib.SequenceMatcher(None, [x[0] + x[1] for x in a], [x[0] + x[1] for x in b], autojunk=False)
    olds, news = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            olds += a[i1:i2]
            news += b[j1:j2]

    def ident(prefix, node):
        if isinstance(node, _FUNC):
            return (prefix, "def", node.name)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return (prefix, "assign", tuple(sorted(_targets(node))))
        return (prefix, "other", id(node))

    groups: dict = {}
    for prefix, _, node in olds:
        groups.setdefault(ident(prefix, node), [None, None])[0] = node
    for prefix, _, node in news:
        groups.setdefault(ident(prefix, node), [None, None])[1] = node

    sel = 0
    notes = []
    for (prefix, kind, name), (o, n) in groups.items():
        where = f"{prefix}{name if kind != 'other' else type(o or n).__name__}"
        if kind == "def":
            if o is not None:
                # Whoever calls the function runs its body.
                sel |= cov.func_lines(path, o.body[0].lineno, o.end_lineno, include_body=True)
                notes.append(f"def-header {where}")
            else:
                bits = 0
                if name.endswith("_sql"):
                    bits |= instantiators(set(static().expr_by_key.get(name[:-4], [])), cov)
                bits |= def_bodies(name, cov)
                if bits:
                    sel |= bits & imp
                    notes.append(f"new-def {where}")
                else:
                    sel |= imp
                    notes.append(f"new-def(unknown) {where}")
            continue
        if kind == "assign":
            node = o if o is not None else n
            span = (path, node.lineno, node.end_lineno) if o is not None else (path, 0, -1)
            exprs = _expr_class_of(path, prefix)
            if exprs and not set(name) & {"_sql_names", "key"}:
                sel |= instantiators(exprs, cov) | readers(set(name), cov, span, tests_only=True)
                notes.append(f"expr-attr {where}")
                continue
            ov = getattr(o, "value", None)
            nv = getattr(n, "value", None)
            if isinstance(ov, ast.Dict) and isinstance(nv, ast.Dict):
                oe, ne = _dict_entries(ov), _dict_entries(nv)
                oset = {(k, sdump(v)) for k, _, v in oe}
                nset = {(k, sdump(v)) for k, _, v in ne}
                changed = {k for k, _, v in oe if (k, sdump(v)) not in nset} | {
                    k for k, _, v in ne if (k, sdump(v)) not in oset}
                bits, ok = 0, True
                for k in changed:
                    ents = [e for e in oe + ne if e[0] == k]
                    kb, kfound = value_signals([e[1] for e in ents], cov)
                    if kfound and _expr_refs_any([e[1] for e in ents]):
                        bits |= kb  # keyed by expression class: only its users see the entry
                        continue
                    vb, vfound = value_signals([e[2] for e in ents], cov)
                    if vfound:
                        bits |= vb | kb
                    else:
                        ok = False
                if ok:
                    sel |= (bits | readers(set(name), cov, span, tests_only=True)) & imp
                    notes.append(f"dict-entries {where}")
                    continue
            sel |= readers(set(name), cov, span) & imp
            notes.append(f"readers {where}")
            continue
        sel |= imp
        notes.append(f"importers {where}")
    return sel, notes


def _expr_refs_any(nodes) -> bool:
    return any(node is not None and _expr_refs(node) for node in nodes)


GRAIN = os.environ.get("BR_GRAIN", "stmt")


def _stmt_spans(func) -> list[tuple[int, int, int, int]]:
    """(first, last, header_last, compound) for every statement in a function body,
    not entering nested functions, classes or lambdas."""
    out = []

    def visit(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append((st.lineno, st.end_lineno, st.end_lineno, 0))
                continue
            children = []
            for field in ("body", "orelse", "finalbody"):
                v = getattr(st, field, None)
                if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                    children.append(v)
            handlers = getattr(st, "handlers", None) or []
            cases = getattr(st, "cases", None) or []
            if children or handlers or cases:
                first_child = min([c[0].lineno for c in children] + [h.lineno for h in handlers]
                                  + [c.pattern.lineno for c in cases])
                out.append((st.lineno, st.end_lineno, max(st.lineno, first_child - 1), 1))
                for c in children:
                    visit(c)
                for h in handlers:
                    out.append((h.lineno, h.end_lineno, max(h.lineno, h.body[0].lineno - 1), 1))
                    visit(h.body)
                for c in cases:
                    out.append((c.pattern.lineno, c.end_lineno if hasattr(c, "end_lineno") else c.body[-1].end_lineno,
                                max(c.pattern.lineno, c.body[0].lineno - 1), 1))
                    visit(c.body)
            else:
                out.append((st.lineno, st.end_lineno, st.end_lineno, 0))

    visit(func.body)
    return out


def _stmt_lines(spans, line: int) -> tuple[int, int] | None:
    """Lines whose execution reveals that the code at `line` ran."""
    best = None
    for sp in spans:
        if sp[0] <= line <= sp[1] and (best is None or sp[1] - sp[0] < best[1] - best[0]):
            best = sp
    if best is None:
        return None
    first, last, header_last, compound = best
    if not compound or line <= header_last:
        return first, header_last
    return first, last  # e.g. an else: line, inside the statement but in no child


def analyze_python(path: str, old: str, new: str, removed, added, cov: CoverageMap, anchors=()) -> tuple[int, str]:
    try:
        new_tree = ast.parse(new)
    except SyntaxError:
        return cov.importers(path), "syntax-error"
    try:
        old_tree = ast.parse(old)
    except SyntaxError:
        return cov.importers(path), "old-unparsable"

    old_scopes = scopes(old_tree)
    new_scopes = scopes(new_tree)
    old_by = {sc.qual: sc for sc in old_scopes}
    new_by = {sc.qual: sc for sc in new_scopes}

    candidates: set[str] = set()
    for ln in removed:
        sc = innermost(old_scopes, ln)
        candidates.add(sc.qual if sc else "")
    for ln in added:
        sc = innermost(new_scopes, ln)
        candidates.add(sc.qual if sc else "")

    sel = 0
    kinds = []
    module_changed = False
    for q in sorted(candidates):
        if q == "":
            continue
        o, n = old_by.get(q), new_by.get(q)
        if o is None or n is None:
            module_changed = True  # scopes shifted; let the skeleton decide too
            continue
        if o.own() == n.own():
            continue
        whole = cov.func_lines(path, o.first, o.last, include_body=o.kind == "def")
        if GRAIN == "stmt" and o.kind == "def":
            spans = _stmt_spans(o.node)
            s = 0
            ok = True
            pts = [ln for ln in removed if o.first <= ln <= o.last]
            for p in anchors:
                if o.first - 1 <= p <= o.last:
                    pts += [p, p + 1]
            if not pts:
                ok = False
            for ln in pts:
                rng = _stmt_lines(spans, ln) if o.first <= ln <= o.last else None
                if rng is None:
                    if ln in removed or not (o.first <= ln <= o.last):
                        if ln in removed:
                            ok = False
                        continue
                    ok = False
                    continue
                s |= cov.func_lines(path, rng[0], rng[1], include_body=True)
            if ok:
                sel |= s & whole if s else 0
                kinds.append(f"stmt {q}")
                continue
        sel |= whole
        kinds.append(f"func {q}")
    if "" in candidates or module_changed:
        bits, notes = module_level(path, old_tree, new_tree, cov)
        sel |= bits
        kinds += notes
    if not kinds:
        return 0, "no-code-change"
    return sel, "; ".join(kinds)


def _config_matters(path: str, fp: FilePatch) -> bool:
    """Whether a pytest config file edit could change any test run."""
    if fp.old_path is None or fp.new_path is None or fp.binary:
        return True
    src_file = SRC / path
    if not src_file.exists():
        return True
    old = src_file.read_text(encoding="utf-8")
    new, _, _, _ = apply_hunks(old, fp.hunks)
    changed = [h[1:] for _, _, body in fp.hunks for h in body if h[:1] in "+-"]
    if any("pytest" in line.lower() or "addopts" in line or "testpaths" in line for line in changed):
        return True
    try:
        if path.endswith(".toml"):
            import tomllib

            tomllib.loads(new)
        else:
            import configparser

            configparser.ConfigParser(interpolation=None, strict=False).read_string(new)
    except Exception:
        return True
    # Does the change land inside a pytest section of the old or new file?
    for text in (old, new):
        section = ""
        for line in text.split("\n"):
            st = line.strip()
            if st.startswith("["):
                section = st
            if "pytest" in section and line in changed:
                return True
    return False


def analyze_file(fp: FilePatch, cov: CoverageMap) -> tuple[int, str]:
    old_path, new_path = fp.old_path, fp.new_path
    path = old_path or new_path
    if path is None:
        return 0, "empty"
    everything = (1 << cov.n) - 1
    name = path.rsplit("/", 1)[-1]
    if name == "conftest.py" or name == "sitecustomize.py" or name == "usercustomize.py":
        return everything, "pytest-hook-file"
    if path in PYTEST_CONFIG:
        return (everything, "pytest-config") if _config_matters(path, fp) else (0, "config-other")
    if old_path is None:
        # A new file: nothing at the pinned commit reads it.
        return 0, "new-file"
    sel = 0
    if new_path != old_path:
        # Deleted or renamed: everything that used the old path loses it.
        if old_path.endswith(".py"):
            return cov.importers(old_path) | cov.openers(old_path), "deleted-or-renamed"
        return cov.openers(old_path), "deleted-or-renamed"
    if fp.binary or not path.endswith(".py"):
        return cov.openers(path), "data-file"
    src_file = SRC / path
    if not src_file.exists():
        return 0, "unknown-file"
    old = src_file.read_text(encoding="utf-8")
    new, removed, added, anchors = apply_hunks(old, fp.hunks)
    sel, kind = analyze_python(path, old, new, removed, added, cov, anchors)
    return sel | cov.openers(path), kind


# ------------------------------------------------------------------------- split


def split(numbers: list[int], durations: list[int]) -> list[list[int]]:
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -durations[n])
    total = sum(durations[n] for n in order)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(order):
            break
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            bins[j].append(n)
            loads[j] += durations[n]
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[0]:
            best = (cost, bins)
        if durations[order[0]] >= total / (k + 1) and k > 1 and cost > best[0]:
            break
    return best[1]


# -------------------------------------------------------------------------- main


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = [0] * len(tests)
    for t in tests:
        durations[t["number"]] = int(t["seconds"] * 1000 + 0.5)
    cov = CoverageMap(MAP)
    valid = (1 << len(tests)) - 1
    for patch in sorted((DATA / "commits").glob("*.patch")):
        sel = 0
        kinds = []
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            for fp in parse_patch(text):
                s, kind = analyze_file(fp, cov)
                sel |= s
                kinds.append(f"{fp.old_path or fp.new_path}:{kind}")
        except Exception as error:  # never crash: run everything instead
            log(f"{patch.stem}: analysis failed ({error!r}); running all tests")
            sel = valid
        sel &= valid
        numbers = [n for n in range(len(tests)) if sel >> n & 1]
        workers = split(numbers, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
        log(f"{patch.stem}: {len(numbers)} tests, {len(workers)} workers; {', '.join(kinds)}")


if __name__ == "__main__":
    main()
