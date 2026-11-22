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
import bisect
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
PYTEST_CONFIG = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "pytest.toml", ".pytest.toml", ".pytest.ini"}


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
        self.listed = raw.get("listed", {})
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

    def listers(self, directory: str) -> int:
        """Tests whose own code listed the directory (os.listdir, scandir, glob)."""
        i = self.listed.get(directory)
        return self.bits(i) if i is not None else 0

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


_SCOPE_WORDS = re.compile(r"\b(?:lambda|def|class)\b")


def scopes(tree: ast.Module, text: str | None = None) -> list[Scope]:
    """Every function and lambda, named by its position among classes and functions.

    With the source text, subtrees spanning no line that mentions def, lambda or
    class are skipped: they hold no scope, so the numbering is unchanged.
    """
    out: list[Scope] = []
    counters: dict[str, int] = {}
    marks: list[int] | None = None
    if text is not None:
        starts = _line_starts(text)
        marks = sorted({bisect.bisect_right(starts, m.start()) for m in _SCOPE_WORDS.finditer(text)})

    AST = ast.AST

    def children(node):
        for field in node._fields:
            v = getattr(node, field, None)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, AST):
                        yield x
            elif isinstance(v, AST):
                yield v

    def visit(node, qual):
        for child in children(node):
            if isinstance(child, (ast.expr_context, ast.operator, ast.cmpop, ast.boolop, ast.unaryop)):
                continue
            if marks is not None:
                lo = getattr(child, "lineno", None)
                if lo is not None:
                    k = bisect.bisect_left(marks, lo)
                    if k == len(marks) or marks[k] > child.end_lineno:
                        continue
            if isinstance(child, _FUNC):
                q = f"{qual}.{child.name}"
                k = counters.get(q, 0)
                counters[q] = k + 1
                q = f"{q}#{k}"
                # The body starts at its first statement, decorators included.
                head = child.body[0]
                first = min([head.lineno] + [d.lineno for d in getattr(head, "decorator_list", ())])
                out.append(Scope(q, child, first, child.end_lineno, "def"))
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
    """Statements with no effect: docstrings, other bare constants, pass."""
    return isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))


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
        self.cls: dict[str, list] = raw.get("cls", {})


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


def readers(names: set[str], cov: CoverageMap, exclude: tuple[str, int, int], tests_only: bool = False,
            split_family: set[str] | None = None):
    """Tests that ran any line mentioning one of the names (as code or in a string).

    tests_only limits the search to test code: tests that inspect a table directly.
    split_family (the files of a dialect family) returns (instance_bits, other_bits):
    instance_bits are lines that reach the name through a lowercase receiver
    (self.X, self.dialect.X, klass.X) or as a bare name inside the family's files;
    only an instance or class of the family can carry the edited value there.
    """
    names = {n for n in names if n}
    if not names:
        return (0, 0) if split_family is not None else 0
    other = 0
    alt = "|".join(sorted(map(re.escape, names)))
    pat = re.compile(r"\b(?:" + alt + r")\b")
    # `NAME = value` or `NAME: annotation` defines the name; only the value can read it.
    definition = re.compile(r"^\s*(?:" + alt + r")\s*(?::[^=]*)?(?:=(?!=)(.*))?$")
    bits = 0
    ex_path, ex_first, ex_last = exclude
    for rel, text in sources().items():
        if tests_only and not rel.startswith("tests/"):
            continue
        if not any(n in text for n in names) or not pat.search(text):
            continue
        starts = None
        seen_lines = set()
        for hit in pat.finditer(text):
            if starts is None:
                starts = _line_starts(text, rel)
            i = bisect.bisect_right(starts, hit.start())  # 1-based line number
            if i in seen_lines:
                continue
            seen_lines.add(i)
            if rel == ex_path and ex_first <= i <= ex_last:
                continue
            line = text[starts[i - 1]: starts[i] - 1 if i < len(starts) else len(text)]
            d = definition.match(line)
            if d and not pat.search(d.group(1) or ""):
                continue
            if split_family is None:
                bits |= cov.at_line(rel, i)
                continue
            instance = True
            for m in pat.finditer(line):
                recv = re.search(r"(\w+)\s*\.\s*$", line[: m.start()])
                if recv:
                    if recv.group(1)[:1].isupper():
                        instance = False  # ClassName.X: read straight off a class
                elif rel not in split_family:
                    instance = False  # a bare name or string elsewhere
            if instance:
                bits |= cov.at_line(rel, i)
            else:
                other |= cov.at_line(rel, i)
    if split_family is not None:
        return bits, other
    return bits


_starts_cache: dict[str, list[int]] = {}


def _line_starts(text: str, cache_key: str | None = None) -> list[int]:
    """Offsets where each line begins; cache_key caches it (for the pinned sources)."""
    if cache_key is not None:
        got = _starts_cache.get(cache_key)
        if got is not None:
            return got
    got = [0]
    find = text.find
    k = find("\n")
    while k != -1:
        got.append(k + 1)
        k = find("\n", k + 1)
    if cache_key is not None:
        _starts_cache[cache_key] = got
    return got


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


_MODATTR: dict = {}


def _modattr():
    """map/modattr.json.gz: per sqlglot module and name, the tests that fetched that
    name from the module object (from-import, attribute, getattr, unpickling)."""
    if "raw" not in _MODATTR:
        p = MAP.parent / "modattr.json.gz"
        _MODATTR["raw"] = json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None
        _MODATTR["bits"] = {}
    return _MODATTR["raw"]


def _modattr_bits(module: str, name: str) -> int:
    raw = _modattr()
    i = raw["keys"].get(module, {}).get(name)
    if i is None:
        return 0
    b = _MODATTR["bits"].get(i)
    if b is None:
        b = _MODATTR["bits"][i] = int(raw["sets"][i], 16)
    return b


def _star_importers(module: str) -> list[str]:
    """Modules that run `from <module> import *` (their namespace copies its names)."""
    out = []
    for p in sorted((SRC / "sqlglot").rglob("__init__.py")):
        text = p.read_text(encoding="utf-8")
        if f"from {module} import *" in text:
            rel = p.relative_to(SRC).parent
            out.append(".".join(rel.parts))
    return out


def _only_removals(old_tree, new_tree) -> bool:
    """The module-level change can only take names away, never fail at import by itself:
    every new top-level statement is an old one, an old `from x import ...` with fewer
    names, or an undecorated function with simple defaults (a rename). Nothing touches
    `from __future__` (that changes how the whole module compiles)."""
    old_dumps = {ast.dump(st) for st in old_tree.body}
    old_from = [st for st in old_tree.body if isinstance(st, ast.ImportFrom)]
    for tree in (old_tree, new_tree):
        for st in tree.body:
            if isinstance(st, ast.ImportFrom) and st.module == "__future__":
                if ast.dump(st) not in {ast.dump(x) for x in new_tree.body} or ast.dump(st) not in old_dumps:
                    return False
    simple = (ast.Constant, ast.Name, ast.Attribute)
    for st in new_tree.body:
        if ast.dump(st) in old_dumps:
            continue
        if isinstance(st, ast.ImportFrom):
            names = {(a.name, a.asname) for a in st.names}
            if any(o.module == st.module and o.level == st.level and names <= {(a.name, a.asname) for a in o.names}
                   for o in old_from):
                continue
            return False
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and not st.decorator_list:
            defaults = st.args.defaults + [d for d in st.args.kw_defaults if d is not None]
            if all(isinstance(d, simple) for d in defaults):
                continue
        return False
    return True


def _gone_names_users(path: str, old_tree, gone: set[str], cov: CoverageMap) -> int | None:
    """Tests that can notice module-level names disappearing from `path`: those that
    fetched a name from the module (or from a package that star-imports it), and
    those that ran a line naming it outside its own binding statement. None when
    that is not enough: classes (namespace scans find them without fetching by
    name), decorated functions, dunder names, and modules with __getattr__,
    __all__ or globals() writes."""
    if os.environ.get("BR_USE_MODATTR", "1") != "1" or _modattr() is None:
        return None
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return None
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    class_names = {k.rsplit(":", 1)[1].rsplit(".", 1)[-1] for k in static().cls}
    for node in ast.walk(old_tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("globals", "vars"):
            return None
    binding: dict[str, ast.stmt] = {}
    for st in old_tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == "__getattr__":
            return None
        if "__all__" in _targets(st) if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else False:
            return None
        names = set()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = {st.name}
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            names = set(_targets(st))
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            names = {(a.asname or a.name).split(".")[0] for a in st.names}
        for n in names & gone:
            binding[n] = st
    sel = 0
    stars = _star_importers(module)
    for n in gone:
        st = binding.get(n)
        if st is None or n.startswith("__") or n in class_names or isinstance(st, ast.ClassDef):
            return None
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.decorator_list:
            return None
        sel |= _modattr_bits(module, n)
        for p in stars:
            sel |= _modattr_bits(p, n)
        # Lines that resolve the name through this module's globals: the module itself
        # and packages that copied its names with a star import. Code elsewhere
        # reaches it by from-import or attribute, which the map records.
        first = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
        pattern = re.compile(rf"\b{re.escape(n)}\b")
        for file, skip in [(path, range(first, st.end_lineno + 1))] + [
            (p.replace(".", "/") + "/__init__.py", ()) for p in stars
        ]:
            try:
                lines = (SRC / file).read_text(encoding="utf-8").split("\n")
            except OSError:
                return None
            for i, line in enumerate(lines, 1):
                if i not in skip and pattern.search(line):
                    sel |= cov.at_line(file, i)
    return sel


def module_level(path: str, old_tree, new_tree, cov: CoverageMap) -> tuple[int, list[str]]:
    import difflib

    a = skeleton(old_tree)
    b = skeleton(new_tree)
    if [x[:2] for x in a] == [x[:2] for x in b]:
        return 0, []
    imp = cov.importers(path)
    gone = _module_names(old_tree) - _module_names(new_tree)
    if gone:
        narrowed = _gone_names_users(path, old_tree, gone, cov) if _only_removals(old_tree, new_tree) else None
        if narrowed is not None:
            return narrowed & imp, [f"module-name-removed(users) {','.join(sorted(gone))[:80]}"]
        # `from module import name` now fails at import time in every importer.
        return imp, [f"module-name-removed {','.join(sorted(gone))[:80]}"]
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
                if n is not None and _runs_at_definition_differs(o, n, new_tree):
                    # Decorators, defaults and (without postponed evaluation) annotations run
                    # when the def statement runs, at import: every importer.
                    sel |= imp
                    notes.append(f"def-statement {where}")
                    continue
                # Whoever calls the function runs its body.
                sel |= cov.func_lines(path, o.body[0].lineno, o.end_lineno, include_body=True)
                notes.append(f"def-header {where}")
            else:
                # A new function runs only if something finds it: an override of a
                # same-named method, generator dispatch by `<key>_sql`, or a decorator.
                if name.startswith("__") and name.endswith("__"):
                    # Python calls protocol methods implicitly (truth value, comparison,
                    # hashing, attribute access, class creation, copying...).
                    sel |= imp
                    notes.append(f"new-dunder {where}")
                    continue
                if path.startswith("tests/") and name.startswith("test"):
                    if _existing_test_name(path, name):
                        sel |= imp  # redefines a test that exists: the later def wins
                        notes.append(f"redefined-test {where}")
                        continue
                    notes.append(f"new-test {where}")  # not in tests.json: cannot fail
                    continue
                if path.startswith("tests/"):
                    # unittest and pytest call hooks (setUp, setUpClass, setUpModule,
                    # setup_method...) and TestCase overrides by name for every test.
                    sel |= imp
                    notes.append(f"new-test-helper {where}")
                    continue
                bits = 0
                if name.endswith("_sql"):
                    bits |= instantiators(set(static().expr_by_key.get(name[:-4], [])), cov)
                bits |= def_bodies(name, cov)
                if n.decorator_list:
                    sel |= imp
                    notes.append(f"new-def(decorated) {where}")
                elif bits:
                    sel |= bits & imp
                    notes.append(f"new-def {where}")
                else:
                    notes.append(f"new-def(unreferenced) {where}")
            continue
        if kind == "assign":
            if (path.startswith("sqlglot/") or path.startswith("tests/")) and any(
                    _mutates_elsewhere(v) for v in (o, n) if v is not None):
                # `exp.X.__repr__ = ...`, `Parser.FUNCTIONS["X"] = ...`, `X |= {...}`: changes
                # state owned elsewhere when the module or class body runs; collection runs
                # it in every worker.
                sel |= (1 << cov.n) - 1
                notes.append(f"mutating-assign {where}")
                continue
            if any(t.startswith("__") and t.endswith("__") for t in name):
                # __slots__, __hash__, __eq__ = ..., __match_args__...: read implicitly.
                sel |= imp
                notes.append(f"dunder-assign {where}")
                continue
            if not prefix and path.startswith("tests/") and any(
                    isinstance(x, ast.Call) for v in (o, n) if v is not None for x in ast.walk(v)):
                sel |= (1 << cov.n) - 1  # a call at a test module's top level runs at collection
                notes.append(f"module-call {where}")
                continue
            if any(t.endswith("_sql") for t in name):
                # Generators dispatch by scanning dir(cls) for `<key>_sql`.
                bits = 0
                for t in name:
                    if t.endswith("_sql"):
                        bits |= instantiators(set(static().expr_by_key.get(t[:-4], [])), cov)
                if not bits:
                    bits = imp
                sel |= (bits | readers(set(name), cov, (path, 0, -1))) & imp
                notes.append(f"sql-dispatch-assign {where}")
                continue
            node = o if o is not None else n
            span = (path, node.lineno, node.end_lineno) if o is not None else (path, 0, -1)
            exprs = _expr_class_of(path, prefix)
            if exprs and not set(name) & {"_sql_names", "key"}:
                cls_name = prefix.rstrip(".").split(".")[-1]
                sel |= instantiators(exprs | _copiers(cls_name, set(name)), cov) | readers(set(name), cov, span, tests_only=True)
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
                added_keys = {k for k, _, _ in ne} - {k for k, _, _ in oe}
                for k in changed:
                    ents = [e for e in oe + ne if e[0] == k]
                    kb, kfound = value_signals([e[1] for e in ents], cov)
                    if kfound and _expr_refs_any([e[1] for e in ents]):
                        bits |= kb  # keyed by expression class: only its users see the entry
                        continue
                    if k in added_keys:
                        # Before, a lookup of this key missed and took some other path:
                        # who built the new value says nothing about who looked it up.
                        ok = False
                        continue
                    vb, vfound = value_signals([e[2] for e in ents], cov)
                    if vfound:
                        bits |= vb | kb
                    else:
                        ok = False
                if ok:
                    fam = family_of(path) if prefix else None
                    if fam:
                        bits &= family_users(fam, cov)
                    sel |= (bits | readers(set(name), cov, span, tests_only=True)) & imp
                    notes.append(f"dict-entries {where}")
                    continue
            fam = family_of(path) if prefix else None
            if fam:
                inst, other = readers(set(name), cov, span, split_family=family_files(fam))
                sel |= (inst & family_users(fam, cov) | other) & imp
                notes.append(f"family-readers {where}")
                continue
            sel |= readers(set(name), cov, span) & imp
            notes.append(f"readers {where}")
            continue
        if (path.startswith("tests/") or path.startswith("sqlglot/")) and (
                (not prefix and _side_effecting(o, n))
                or (prefix and _class_body_side_effect(o, n, prefix, old_tree, new_tree))):
            # pytest imports every test module (and what they import at the top) into each
            # worker before any test runs: a module-level statement's side effects (logging,
            # warnings, patches, registries) reach every test, not just the importers.
            sel |= (1 << cov.n) - 1
            notes.append(f"module-statement {where}")
            continue
        sel |= imp
        notes.append(f"importers {where}")
    return sel, notes


def _is_type_checking_if(node) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test.operand if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not) else node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _mutates_elsewhere(node) -> bool:
    """An assignment whose target is an attribute or subscript (state of another
    object), or an augmented assignment (which can mutate a shared object in place)."""
    if isinstance(node, ast.AugAssign):
        return True
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    for t in targets:
        if t is None:
            continue
        for x in ast.walk(t):
            if isinstance(x, (ast.Attribute, ast.Subscript)):
                return True
    return False


def _class_node(tree, prefix: str):
    names = prefix.rstrip(".").split(".")
    body = tree.body
    node = None
    for nm in names:
        node = next((st for st in body if isinstance(st, ast.ClassDef) and st.name == nm), None)
        if node is None:
            return None
        body = node.body
    return node


def _class_body_side_effect(o, n, prefix, old_tree, new_tree) -> bool:
    """A non-definition statement in a class body runs when the class is created, in
    every worker at collection. Only a method call on a name the same class body binds
    (redshift's `KEYWORDS.pop(...)` on its own fresh dict) stays local."""
    for node, tree in ((o, old_tree), (n, new_tree)):
        if node is None:
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            cls = _class_node(tree, prefix)
            own = set()
            if cls is not None:
                for st in cls.body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)):
                        tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                        own |= {t.id for t in tg if isinstance(t, ast.Name)}
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in own:
                continue
            return True
        if isinstance(node, (ast.Expr,)) and not any(isinstance(x, (ast.Call, ast.Await)) for x in ast.walk(node)):
            continue  # a bare constant or name
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        return True
    return False


def _is_main_guard(node) -> bool:
    """`if __name__ == "__main__":` runs only when the file is executed as a script."""
    t = getattr(node, "test", None)
    return (isinstance(node, ast.If) and isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
            and t.left.id == "__name__" and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value == "__main__")


def _side_effecting(o, n) -> bool:
    """A module-level statement that can change process-wide state when it runs: a call
    expression, a loop, with, try, or an if (other than an `if TYPE_CHECKING:` block of
    imports, whose effect is importing pinned modules)."""
    for node in (o, n):
        if node is None:
            continue
        if _is_type_checking_if(node) or _is_main_guard(node):
            continue
        if isinstance(node, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.If,
                             ast.Delete, ast.Global, ast.Nonlocal, ast.Assert, ast.Raise)):
            return True
        if isinstance(node, ast.Expr) and any(isinstance(x, (ast.Call, ast.Await)) for x in ast.walk(node)):
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
    return False


_COPIERS: dict = {}


def _copiers(cls_name: str, names: set[str]) -> set[str]:
    """Expression class keys whose class body reads `<cls_name>.<name>` (e.g. Revoke's
    `arg_types = {**Grant.arg_types, ...}`): they carry the edited value too."""
    if "index" not in _COPIERS:
        index: dict = {}
        for rel, text in sources().items():
            if not rel.startswith("sqlglot/expressions/") or ".arg_types" not in text and "." not in text:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                            index.setdefault((sub.value.id, sub.attr), set()).add((rel, node.name))
        _COPIERS["index"] = index
    keys: set[str] = set()
    for nm in names:
        for rel, other in _COPIERS["index"].get((cls_name, nm), ()):
            if other == cls_name:
                continue
            for k in static().expr_by_name.get(other, []):
                if static().expr[k][0] == rel:
                    keys.add(k)
    return keys


def _literal_or_none(node):
    if node is None:
        return True, None
    try:
        return True, ast.literal_eval(node)
    except Exception:
        return False, None


def _postponed_annotations(tree) -> bool:
    return any(isinstance(st, ast.ImportFrom) and st.module == "__future__"
               and any(a.name == "annotations" for a in st.names) for st in tree.body)


def _runs_at_definition_differs(o, n, new_tree) -> bool:
    """Whether code that runs when the def statement runs differs: decorators, default
    values that are not both literals, and annotations unless they are postponed."""
    # property, classmethod and staticmethod only build a descriptor: adding or removing
    # one changes what callers get, and every such caller ran the body.
    def decorators(f):
        return [sdump(d) for d in f.decorator_list
                if not (isinstance(d, ast.Name) and d.id in ("property", "classmethod", "staticmethod"))]
    if decorators(o) != decorators(n):
        return True
    od = list(o.args.defaults) + list(o.args.kw_defaults)
    nd = list(n.args.defaults) + list(n.args.kw_defaults)
    if len(od) != len(nd):
        return any(not _literal_or_none(x)[0] for x in od + nd)
    for a, b in zip(od, nd):
        if (a is None) != (b is None) or (a is not None and sdump(a) != sdump(b)):
            if not (_literal_or_none(a)[0] and _literal_or_none(b)[0]):
                return True
    if not _postponed_annotations(new_tree):
        def anns(f):
            args = f.args.posonlyargs + f.args.args + f.args.kwonlyargs + [f.args.vararg, f.args.kwarg]
            return [sdump(x.annotation) if x is not None and x.annotation is not None else None for x in args] + [
                sdump(f.returns) if f.returns is not None else None]
        if anns(o) != anns(n):
            return True
    return False


_TEST_IDS = None


def _existing_test_name(path: str, name: str) -> bool:
    global _TEST_IDS
    if _TEST_IDS is None:
        try:
            _TEST_IDS = {t["id"] for t in json.loads((DATA / "tests.json").read_text())["tests"]}
        except Exception:
            _TEST_IDS = set()
            return True  # cannot tell: assume it exists
    return any(i.startswith(path + "::") and i.endswith("::" + name) for i in _TEST_IDS)


FAMILY_PKGS = ("dialects", "parsers", "generators", "typing")


def family_of(path: str) -> str | None:
    """'mysql' for sqlglot/{dialects,parsers,generators,typing}/mysql.py."""
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILY_PKGS and parts[2].endswith(".py"):
        name = parts[2][:-3]
        if name not in ("dialect", "base", "__init__"):
            return name
    return None


def family_files(name: str) -> set[str]:
    return {f"sqlglot/{p}/{name}.py" for p in FAMILY_PKGS}


def family_users(name: str, cov: CoverageMap) -> int:
    """Tests that instantiated a class of the family (dialect, tokenizer, parser,
    generator) or of a subclass, e.g. Doris for MySQL."""
    mods = {f"sqlglot.{p}.{name}" for p in FAMILY_PKGS}
    bits = 0
    for k, desc in static().cls.items():
        if k.split(":", 1)[0] in mods:
            for d in desc:
                bits |= cov.class_users(d)
    return bits


def _module_names(tree: ast.Module) -> set[str]:
    """Names bound at module level (defs, classes, assignments, imports)."""
    names: set[str] = set()

    def visit(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(st.name)
            elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                names.update(_targets(st))
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(st, (ast.If, ast.Try)):
                visit(st.body)
                visit(getattr(st, "orelse", []))
                for h in getattr(st, "handlers", []):
                    visit(h.body)
                visit(getattr(st, "finalbody", []))

    visit(tree.body)
    return names


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
                # Decorators sit above the def line and run when the def statement does.
                first = min([st.lineno] + [d.lineno for d in st.decorator_list])
                out.append((first, st.end_lineno, st.end_lineno, 0))
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


_SESSION_ONLY: dict = {}


def session_only_lines(path: str) -> set[int]:
    """Lines a pytest session runs in tests the isolated per-test map did not record
    (map/session_only.json)."""
    if "lines" not in _SESSION_ONLY:
        try:
            raw = json.loads((Path(MAP).parent / "session_only.json").read_text())["lines"]
        except Exception:
            raw = {}
        _SESSION_ONLY["lines"] = {p: set(v) for p, v in raw.items()}
    return _SESSION_ONLY["lines"].get(path, set())


def _depth(spans, line: int) -> int:
    """How many compound statements' bodies contain the line."""
    return sum(1 for first, last, header_last, compound in spans if compound and header_last < line <= last)


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

    old_scopes = scopes(old_tree, old)
    new_scopes = scopes(new_tree, new)
    old_by = {sc.qual: sc for sc in old_scopes}
    new_by = {sc.qual: sc for sc in new_scopes}

    candidates: set[str] = set()

    def add(scs, ln):
        # A line where a scope starts (a lambda inside a dict entry, a one-line
        # def) also holds code of the enclosing scope: count both.
        sc = innermost(scs, ln)
        candidates.add(sc.qual if sc else "")
        while sc is not None and sc.node.lineno == ln:
            sc = innermost([x for x in scs if x is not sc and x.first <= sc.node.lineno and sc.node.end_lineno <= x.last], ln)
            candidates.add(sc.qual if sc else "")

    for ln in removed:
        add(old_scopes, ln)
    for ln in added:
        add(new_scopes, ln)

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
            if any(o.first - 1 <= p <= o.last for p in anchors):
                # Inserted lines may sit at a shallower nesting than either neighbour (after
                # a nested block ends, or before an elif/except), so a neighbour's tests
                # are not all the runs that reach them: use the whole function.
                ok = False
            if not pts:
                ok = False
            # A replaced line that moves to another nesting (a dedent out of an if body)
            # runs for tests that never ran the old line: use the whole function.
            new_spans = _stmt_spans(n.node)
            old_depths = {_depth(spans, ln) for ln in removed if o.first <= ln <= o.last}
            new_depths = {_depth(new_spans, ln) for ln in added if n.first <= ln <= n.last}
            if old_depths != new_depths:
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
                part = cov.func_lines(path, rng[0], rng[1], include_body=True)
                if set(range(rng[0], rng[1] + 1)) & session_only_lines(path):
                    # Run under pytest -n by tests whose isolated run never reached it
                    # (state left by earlier tests in the worker): every caller.
                    ok = False
                s |= part
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


def _shadows_a_module(path: str) -> bool:
    """A new .py file that can change what an import statement resolves to: anything
    directly at the repository root (it is first on sys.path), a new package directory's
    __init__.py at the root, or a module or package whose dotted name already exists."""
    parts = path.split("/")
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "__init__.py" and not (SRC / parts[0]).is_dir()):
        return True
    if parts[-1] == "__init__.py":
        stem = "/".join(parts[:-1])
        return (SRC / (stem + ".py")).exists()
    stem = path[:-3]
    return (SRC / stem).is_dir()


_PROCESS_WIDE = re.compile(
    r"\blogging\.(?:disable|basicConfig|captureWarnings|root\b)|\.setLevel\(|\bwarnings\.|\bos\.environ\b"
    r"|\bos\.(?:putenv|unsetenv|chdir)\(|\bsys\.(?:setrecursionlimit|path|modules|settrace|setprofile|stdout|stderr)\b"
    r"|\.start\(\)|\bsetattr\(|\bdelattr\(|\bgc\.(?:disable|freeze)\(")


_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _coding_changed(old: str, new: str) -> bool:
    """PEP 263: a coding declaration in the first two lines changes how the file is read."""
    o, n = old.splitlines()[:2], new.splitlines()[:2]
    return o != n and any(_CODING.match(line) for line in o + n)


def analyze_file(fp: FilePatch, cov: CoverageMap) -> tuple[int, str]:
    old_path, new_path = fp.old_path, fp.new_path
    path = old_path or new_path
    if path is None:
        return 0, "empty"
    everything = (1 << cov.n) - 1
    name = path.rsplit("/", 1)[-1]
    if name == "conftest.py" or name == "sitecustomize.py" or name == "usercustomize.py":
        return everything, "pytest-hook-file"
    in_rootdir_search = "/" not in path or (path.startswith("tests/") and path.count("/") == 1)
    if name in PYTEST_CONFIG and in_rootdir_search:
        # pytest looks for its config in tests/ (the argument) and then the repository root.
        return (everything, "pytest-config") if _config_matters(path, fp) else (0, "config-other")
    if name.endswith(".pth") or (old_path is None and path.endswith(".py") and _shadows_a_module(path)):
        # A new top-level module (or package) shadows any third-party or sqlglot module of
        # that name for every test; a new package __init__ shadows a same-named module.
        return everything, "new-module-shadow"
    if old_path is None:
        # A new file: only code that lists its directory can find it.
        return cov.listers(new_path.rsplit("/", 1)[0] if "/" in new_path else ""), "new-file"
    sel = 0
    if new_path != old_path:
        # Deleted or renamed: everything that used the old path loses it.
        listed = cov.listers(old_path.rsplit("/", 1)[0] if "/" in old_path else "")
        if new_path:
            listed |= cov.listers(new_path.rsplit("/", 1)[0] if "/" in new_path else "")
        if old_path.endswith(".py"):
            return cov.importers(old_path) | cov.openers(old_path) | listed, "deleted-or-renamed"
        return cov.openers(old_path) | listed, "deleted-or-renamed"
    if fp.binary or not path.endswith(".py"):
        return cov.openers(path), "data-file"
    src_file = SRC / path
    if not src_file.exists():
        return 0, "unknown-file"
    old = src_file.read_text(encoding="utf-8")
    new, removed, added, anchors = apply_hunks(old, fp.hunks)
    if path.startswith("tests/"):
        changed = [h[1:] for _, _, body in fp.hunks for h in body if h[:1] in "+-"]
        if any(_PROCESS_WIDE.search(line) for line in changed):
            # A test that changes process-wide state without restoring it affects later
            # tests in the same worker, whichever they are.
            return everything, "process-wide-state"
    if _coding_changed(old, new):
        return cov.importers(path) | cov.openers(path), "coding-declaration"
    sel, kind = analyze_python(path, old, new, removed, added, cov, anchors)
    if path.startswith("sqlglot/typing/") and kind != "no-code-change":
        # test_no_redundant_expression_metadata compares the annotators' code objects
        # without calling them: coverage cannot link it to these files.
        sel |= _tests_named("tests/test_typing.py::")
    return sel | cov.openers(path), kind


def _tests_named(prefix: str) -> int:
    bits = 0
    try:
        for t in json.loads((DATA / "tests.json").read_text())["tests"]:
            if t["id"].startswith(prefix):
                bits |= 1 << t["number"]
    except Exception:
        pass
    return bits


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
