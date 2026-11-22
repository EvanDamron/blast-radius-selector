"""Blast Radius selector: coverage candidates, narrowed by comparison-outcome
records and by running small candidate sets against the patched code.

Stage 1 (base_selector): the coverage/AST selector picks every test that could
observe the change. Its answer is the fallback at every later step.
Stage 2 (comparison records): when the only change in a function is the operator
of one comparison, keep the tests in which the two operators gave different
results on the operands seen at the pinned commit (map/cmp.json.gz).
Stage 3 (execution): when the candidates are cheap enough that running them costs
less than the worker they would occupy, apply the patch in memory, run them in a
forked child and keep exactly the ones that fail.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import select
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import base_selector as base  # noqa: E402

DATA = base.DATA
SRC = base.SRC
CMP_MAP = Path(os.environ.get("BR_CMP", str(HERE / "map" / "cmp.json.gz")))
REL_MAP = Path(os.environ.get("BR_REL", str(HERE / "map" / "rel.json.gz")))
WORKER_COST = 2000
MAX_WORKERS = 8

# Stage switches (for measurement).
USE_CMP = os.environ.get("BR_USE_CMP", "1") == "1"
USE_EXEC = os.environ.get("BR_USE_EXEC", "0") == "1"
# Run candidates when their recorded time is at most this (ms).
EXEC_LIMIT_MS = int(os.environ.get("BR_EXEC_LIMIT_MS", "2500"))
# Loading the TPC tables (TestExecutor.setUpClass) costs about this much in a fresh child (ms).
EXECUTOR_SETUP_MS = 1500
# Fork, imports and lazy dialect loading in a fresh child (ms).
EXEC_OVERHEAD_MS = int(os.environ.get("BR_EXEC_OVERHEAD_MS", "250"))
EXEC_SHARE = float(os.environ.get("BR_EXEC_SHARE", "0.45"))
# Stop starting new executions after this much wall time (s).
WALL_BUDGET = float(os.environ.get("BR_WALL_BUDGET", "100"))
# Tests that start their own interpreter on the pinned checkout: never executed here.
NO_EXEC_IDS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}

log = base.log

# One parse per distinct source text: the base selector and the stages below parse the
# same old and new files. The trees are only read.
_parse_cache: dict[str, ast.Module] = {}
_ast_parse = ast.parse


def _cached_parse(source, *args, **kwargs):
    if args or kwargs or type(source) is not str:
        return _ast_parse(source, *args, **kwargs)
    tree = _parse_cache.get(source)
    if tree is None:
        if len(_parse_cache) > 64:
            _parse_cache.clear()
        tree = _parse_cache[source] = _ast_parse(source)
    return tree


ast.parse = _cached_parse


# ------------------------------------------------------------------ candidates


class Commit:
    def __init__(self, cid: str):
        self.id = cid
        self.sel = 0
        self.file_sel: dict[str, int] = {}  # per changed Python file: its own candidates
        self.other_sel = 0                  # candidates from every other changed file
        self.openers: dict[str, int] = {}   # per changed Python file: tests that read it as a file
        self.removed: dict[str, list[int]] = {}  # per changed Python file: old lines removed
        self.tag: dict[str, str] = {}       # per changed Python file: the base selector's evidence
        self.cmp_like: dict[str, bool] = {}  # per changed Python file: only comparison operators differ
        self.kinds: list[str] = []
        self.overlay: dict[str, str] = {}
        self.old: dict[str, str] = {}
        self.fixtures: dict[str, tuple[str, str]] = {}  # per changed SQL fixture: (old, new) text
        self.changed: dict[str, set[int]] = {}  # per changed Python file: old lines removed or beside an insertion
        self.executable = True  # every changed file is an existing Python file
        self.note = ""


_OPERATOR = __import__("re").compile(r"\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>")


def _only_operators_differ(hunks) -> bool:
    """Cheap test on the patch text: every removed line pairs with an added line that is
    the same once comparison operators are blanked. Decides whether the AST comparison
    in narrow_by_comparisons is worth running."""
    for _, _, body in hunks:
        removed = [h[1:] for h in body if h[:1] == "-"]
        added = [h[1:] for h in body if h[:1] == "+"]
        if len(removed) != len(added):
            return False
        for r, a in zip(removed, added):
            if _OPERATOR.sub("\0", r) != _OPERATOR.sub("\0", a):
                return False
    return True


def analyze(cid: str, text: str, cov, valid: int) -> Commit:
    c = Commit(cid)
    try:
        for fp in base.parse_patch(text):
            s, kind = base.analyze_file(fp, cov)
            c.sel |= s
            path = fp.old_path or fp.new_path
            c.kinds.append(f"{path}:{kind}")
            if (
                path is not None
                and fp.old_path == fp.new_path
                and not fp.binary
                and path.startswith("tests/fixtures/")
                and path.endswith(".sql")
                and kind.startswith("data-file")
                and (SRC / path).exists()
            ):
                old = (SRC / path).read_text(encoding="utf-8")
                new, _, _, _ = base.apply_hunks(old, fp.hunks)
                c.fixtures[path] = (old, new)
                c.file_sel[path] = c.file_sel.get(path, 0) | (s & valid)
                c.executable = False
                continue
            if (
                path is None
                or fp.old_path != fp.new_path
                or fp.binary
                or not path.endswith(".py")
                or not (path.startswith("sqlglot/") or path.startswith("tests/"))
                or not (SRC / path).exists()
            ):
                if not (s == 0 and kind in ("empty", "config-other", "unknown-file")):
                    c.executable = False
                c.other_sel |= s
                continue
            old = (SRC / path).read_text(encoding="utf-8")
            new, removed, _, anchors = base.apply_hunks(old, fp.hunks)
            c.removed[path] = sorted(removed)
            c.changed[path] = set(removed) | {a for p in anchors for a in (p, p + 1)}
            c.old[path] = old
            c.overlay[path] = new
            c.file_sel[path] = c.file_sel.get(path, 0) | (s & valid)
            c.tag[path] = kind.split(" ", 1)[0]
            c.cmp_like[path] = _only_operators_differ(fp.hunks)
            c.openers[path] = cov.openers(path) & valid
    except Exception as error:  # never crash: run everything instead
        log(f"{cid}: analysis failed ({error!r}); running all tests")
        c.sel = valid
        c.other_sel = valid
        c.executable = False
    c.sel &= valid
    c.other_sel &= valid
    return c


def narrow_file(c: Commit, path: str, keep: int, how: str) -> None:
    """Intersect one changed file's candidates with `keep`. Each changed file selects on
    its own, so narrowing one file leaves the others' candidates in place."""
    before = c.file_sel[path]
    # A test that reads the source file itself sees any edit to its text.
    after = before & (keep | c.openers.get(path, 0))
    if after != before:
        c.file_sel[path] = after
        total = c.other_sel
        for b in c.file_sel.values():
            total |= b
        c.note += f" {how}:{c.sel.bit_count()}->{total.bit_count()}"
        c.sel = total


# -------------------------------------------------------- comparison records

_CMP_OPS = [ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot]


class CmpMap:
    def __init__(self, path: Path):
        with gzip.open(path, "rb") as fh:
            raw = json.loads(fh.read())
        self.sets = raw["sets"]
        self.sites = raw["sites"]
        self._bits: dict[int, int] = {}

    def bits(self, i: int) -> int:
        b = self._bits.get(i)
        if b is None:
            b = self._bits[i] = int(self.sets[i], 16)
        return b

    def differing(self, path: str, line: int, col: int, new_op: int) -> int | None:
        """Tests in which new_op gave a different result than the original operator at
        this comparison, or None when the site has no record."""
        site = self.sites.get(path, {}).get(f"{line}:{col}")
        if site is None:
            return None
        i = site[new_op + 1]
        return self.bits(i) if i >= 0 else 0


class RelMap:
    """map/rel.json.gz: for each comparison and each test, the relation signatures of the
    operands that test produced (T/F/X/N for ==, !=, <, <=, >, >=, is, is not)."""

    def __init__(self, path: Path):
        with gzip.open(path, "rb") as fh:
            raw = json.loads(fh.read())
        self.sets = raw["sets"]
        self.sites = raw["sites"]
        self._bits: dict[int, int] = {}

    def bits(self, i: int) -> int:
        b = self._bits.get(i)
        if b is None:
            b = self._bits[i] = int(self.sets[i], 16)
        return b

    def differing(self, path: str, line: int, col: int, new_op: int, old_op: int) -> int | None:
        """Tests that produced operands on which the two relations do not agree (or on
        which either could not be evaluated to a bool). None when the site never ran."""
        site = self.sites.get(path, {}).get(f"{line}:{col}")
        if site is None:
            return None
        out = 0
        for sig, i in site.items():
            if sig == "*" or sig[old_op] != sig[new_op] or sig[old_op] not in "TF":
                out |= self.bits(i)
        return out


def _single_op_change(old_tree: ast.AST, new_tree: ast.AST, lines: list[int]):
    """If the two trees differ only in the operator of single-operator comparisons,
    return [(old Compare node, new operator index)], else None.

    `lines` are the changed line numbers, the same in both files (each removed line was
    replaced by one added line). Subtrees that span none of them have identical text
    and are not compared.
    """
    lo, hi = lines[0], lines[-1]
    changed = set(lines)

    def touched(node) -> bool:
        first = getattr(node, "lineno", None)
        if first is None:
            return True
        last = node.end_lineno
        if last < lo or first > hi:
            return False
        return any(first <= ln <= last for ln in changed)

    out = []
    stack = [(old_tree, new_tree)]
    while stack:
        a, b = stack.pop()
        if type(a) is not type(b):
            return None
        if isinstance(a, ast.Compare):
            if len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
                if type(a.ops[0]) not in _CMP_OPS or type(b.ops[0]) not in _CMP_OPS:
                    return None
                out.append((a, _CMP_OPS.index(type(b.ops[0]))))
                stack.append((a.left, b.left))
                stack.append((a.comparators[0], b.comparators[0]))
                continue
        for name in a._fields:
            x, y = getattr(a, name, None), getattr(b, name, None)
            if isinstance(x, list):
                if not isinstance(y, list) or len(x) != len(y):
                    return None
                for p, q in zip(x, y):
                    if isinstance(p, ast.AST):
                        if touched(p) or touched(q):
                            stack.append((p, q))
                    elif p != q:
                        return None
            elif isinstance(x, ast.AST):
                if not isinstance(y, ast.AST):
                    return None
                if touched(x) or touched(y):
                    stack.append((x, y))
            elif x != y:
                return None
    return out


def narrow_by_comparisons(c: Commit, cmp_map: CmpMap) -> None:
    """For each changed file whose only change is comparison operators, keep the tests
    in which the old and new operator disagreed."""
    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or not c.file_sel.get(path) or not c.cmp_like.get(path) or not c.removed.get(path):
            continue
        try:
            changes = _single_op_change(ast.parse(c.old[path]), ast.parse(new), c.removed[path])
        except SyntaxError:
            continue
        if not changes:
            continue
        keep = 0
        for node, new_op in changes:
            if isinstance(cmp_map, RelMap):
                d = cmp_map.differing(path, node.lineno, node.col_offset, new_op, _CMP_OPS.index(type(node.ops[0])))
            else:
                d = cmp_map.differing(path, node.lineno, node.col_offset, new_op)
            if d is None:
                # The comparison never ran in any test process: nothing can observe it.
                continue
            keep |= d
        narrow_file(c, path, keep, "cmp")


# ------------------------------------------------- attribute and dict-key records

ATTR_MAP = Path(os.environ.get("BR_ATTR", str(HERE / "map" / "attr.json.gz")))
DICT_MAP = Path(os.environ.get("BR_DICT", str(HERE / "map" / "dict.json.gz")))
USE_ATTR = os.environ.get("BR_USE_ATTR", "1") == "1"
USE_DICT = os.environ.get("BR_USE_DICT", "1") == "1"


class SetMap:
    def __init__(self, path: Path):
        with gzip.open(path, "rb") as fh:
            self.raw = json.loads(fh.read())
        self.sets = self.raw["sets"]
        self._bits: dict[int, int] = {}

    def bits(self, i: int | None) -> int:
        if i is None or i < 0:
            return 0
        b = self._bits.get(i)
        if b is None:
            b = self._bits[i] = int(self.sets[i], 16)
        return b


_maps: dict[str, SetMap | None] = {}


def _map(path: Path) -> SetMap | None:
    key = str(path)
    if key not in _maps:
        _maps[key] = SetMap(path) if path.exists() else None
    return _maps[key]


def class_level_change(old_tree: ast.Module, new_tree: ast.Module):
    """(class path, old statement, new statement, class node) when the two modules differ
    in exactly one statement of a class body (outside any function), else None."""
    changes = []

    def walk(a_body, b_body, qual, owner) -> bool:
        if len(a_body) != len(b_body):
            return False
        for a, b in zip(a_body, b_body):
            if type(a) is not type(b):
                return False
            if isinstance(a, ast.ClassDef):
                if a.name != b.name or ast.dump(ast.Tuple(elts=a.bases + a.decorator_list + [k.value for k in a.keywords])) != ast.dump(
                    ast.Tuple(elts=b.bases + b.decorator_list + [k.value for k in b.keywords])
                ):
                    return False
                if not walk(a.body, b.body, qual + [a.name], a):
                    return False
            elif ast.dump(a) != ast.dump(b):
                if not qual:
                    return False
                changes.append((qual, a, b, owner))
                if len(changes) > 1:
                    return False
        return True

    if not walk(old_tree.body, new_tree.body, [], None) or len(changes) != 1:
        return None
    return changes[0]


def _assign(st):
    """(name, value) of a plain `NAME = value` / `NAME: T = value` statement."""
    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
        return st.targets[0].id, st.value
    if isinstance(st, ast.AnnAssign) and st.value is not None and isinstance(st.target, ast.Name):
        return st.target.id, st.value
    return None, None


def _dict_rule(site: str, old: ast.Dict, new: ast.Dict) -> int | None:
    """Tests that can observe an edit to entries of a class-level dict literal."""
    m = _map(DICT_MAP)
    if m is None:
        return None
    info = m.raw["sites"].get(site)
    keys = m.raw["keys"].get(site)
    if info is None or keys is None:
        return None
    old_items = [("*", ast.dump(v)) if k is None else ("k", ast.dump(k)) for k, v in zip(old.keys, old.values)]
    new_items = [("*", ast.dump(v)) if k is None else ("k", ast.dump(k)) for k, v in zip(new.keys, new.values)]
    old_explicit = [(ast.dump(k), ast.dump(v)) for k, v in zip(old.keys, old.values) if k is not None]
    if len(old_explicit) != len(keys):
        return None
    old_keys = [k for k, _ in old_explicit]
    new_pairs = [(ast.dump(k), ast.dump(v), k) for k, v in zip(new.keys, new.values) if k is not None]
    new_keys = [k for k, _, _ in new_pairs]
    if len(set(old_keys)) != len(old_keys) or len(set(new_keys)) != len(new_keys):
        return None
    old_set, new_set = set(old_keys), set(new_keys)
    # Entries that stay must keep their order, also relative to the ** spreads.
    if [x for x in old_items if x[0] == "*" or x[1] in new_set] != [x for x in new_items if x[0] == "*" or x[1] in old_set]:
        return None
    new_value = {k: v for k, v, _ in new_pairs}
    v, h = info["v"], info["h"]
    affected = m.bits(info["a"]) | m.bits(v.get("?")) | m.bits(h.get("?"))
    structural = False
    for i, (k, value) in enumerate(old_explicit):
        canon = keys[i]
        if canon == "?":
            return None
        if k not in new_set:
            affected |= m.bits(v.get(canon)) | m.bits(h.get(canon))
            structural = True
        elif new_value[k] != value:
            affected |= m.bits(v.get(canon))
    for k, _, node in new_pairs:
        if k in old_set:
            continue
        if isinstance(node, ast.Constant) and type(node.value) is str:
            canon = "s" + node.value
        elif isinstance(node, ast.Constant) and (type(node.value) in (int, bool) or node.value is None):
            canon = repr(node.value)
        else:
            return None
        affected |= m.bits(v.get(canon)) | m.bits(h.get(canon))
        structural = True
    if structural:
        affected |= m.bits(info["k"]) | m.bits(info["l"])
    return affected


def _attr_rule(path: str, qual: list[str], name: str, cls: ast.ClassDef) -> int | None:
    """Tests whose reads of NAME resolved to this class's definition."""
    m = _map(ATTR_MAP)
    if m is None:
        return None
    raw = m.raw
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    cls_name = ".".join(qual)
    owner = f"{module}:{cls_name}"
    if not hasattr(m, "strings"):
        m.strings, m.special, m.classes = set(raw["strings"]), set(raw["special"]), set(raw["classes"])
    if name in m.strings or name.startswith("__") or owner in m.special or owner not in m.classes:
        return None
    if name not in raw["defs"].get(path, {}).get(cls_name, ()):
        return None
    if cls.decorator_list or cls.keywords:
        return None
    # The class body must define the name once and never use it by bare name.
    defined = 0
    stack: list = list(cls.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(node.decorator_list if not isinstance(node, ast.Lambda) else [])
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.ClassDef):
            stack.extend(node.bases)
            stack.extend(node.decorator_list)
            continue
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, ast.Load):
                return None
            defined += 1
        stack.extend(ast.iter_child_nodes(node))
    if defined != 1:
        return None
    return m.bits(raw["attr"].get(f"{owner}.{name}"))


# Evidence tags of the base selector that mean the change is inside a function body.
_FUNCTION_TAGS = {"stmt", "func", "no-code-change", "syntax-error", "def-header"}


def narrow_by_reads(c: Commit) -> None:
    """A file whose only change is one class-level assignment keeps the tests that read it."""
    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or not c.file_sel.get(path) or c.tag.get(path) in _FUNCTION_TAGS:
            continue
        try:
            change = class_level_change(ast.parse(c.old[path]), ast.parse(new))
        except SyntaxError:
            continue
        if change is None:
            continue
        qual, a, b, cls = change
        name, old_value = _assign(a)
        name2, new_value = _assign(b)
        if name is None or name != name2:
            continue
        affected = None
        how = ""
        defined = sum(1 for st in cls.body if _assign(st)[0] == name)
        if defined != 1:
            # Two literals under one name share a record: do not guess which is which.
            continue
        if USE_DICT and isinstance(old_value, ast.Dict) and isinstance(new_value, ast.Dict):
            affected = _dict_rule(f"{path}|{'.'.join(qual)}.{name}", old_value, new_value)
            how = "dict"
        if affected is None and USE_ATTR:
            affected = _attr_rule(path, qual, name, cls)
            how = "attr"
        if affected is not None:
            narrow_file(c, path, affected, how)


# ------------------------------------------------------------ default values

USE_DEFAULTS = os.environ.get("BR_USE_DEFAULTS", "1") == "1"
USE_FIXTURES = os.environ.get("BR_USE_FIXTURES", "1") == "1"
DEFAULTS_MAP = HERE / "map" / "defaults.json.gz"
_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _literal(node) -> tuple[bool, object]:
    """(True, value) for a default the recorder tracked (tools/defaults/build_table.py)."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return True, node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)):
        return True, -node.operand.value
    return False, None


def _function_defaults(tree: ast.Module) -> tuple[str, dict]:
    """The module dumped with every literal default blanked, and {qualname: [{param:
    (type, value)}, ...]} for those defaults, one dict per definition of the qualname."""
    found: dict = {}

    def walk(body, qual):
        for node in body:
            if isinstance(node, _FUNC):
                q = qual + node.name
                a = node.args
                pos = a.posonlyargs + a.args
                pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults))
                pairs += [(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
                lits = {}
                for arg, d in pairs:
                    ok, v = _literal(d)
                    if ok:
                        lits[arg.arg] = (type(v).__name__, v)
                found.setdefault(q, []).append(lits)
                walk(node.body, q + ".<locals>.")
            elif isinstance(node, ast.ClassDef):
                walk(node.body, qual + node.name + ".")
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(node, field, None)
                    if isinstance(sub, list):
                        walk(sub, qual)

    walk(tree.body, "")

    class Blank(ast.NodeTransformer):
        def visit_arguments(self, node):
            node.defaults = [ast.Constant("<default>") if _literal(d)[0] else d for d in node.defaults]
            node.kw_defaults = [
                ast.Constant("<default>") if d is not None and _literal(d)[0] else d for d in node.kw_defaults
            ]
            self.generic_visit(node)
            return node

    import copy

    return ast.dump(Blank().visit(copy.deepcopy(tree))), found


def narrow_by_defaults(c: Commit) -> None:
    """A file whose only change is literal default values keeps the tests in which some
    call of that function may have used the default: a call that received any other
    value passed the argument itself (map/defaults.json.gz)."""
    m = _map(DEFAULTS_MAP)
    if m is None:
        return
    for path, new in c.overlay.items():
        # Only a function-header edit can be a default change (base selector evidence).
        if not c.file_sel.get(path) or not any(k.startswith(f"{path}:") and "def-header" in k for k in c.kinds):
            continue
        try:
            old_dump, old_defs = _function_defaults(ast.parse(c.old[path]))
            new_dump, new_defs = _function_defaults(ast.parse(new))
        except SyntaxError:
            continue
        if old_dump != new_dump:
            continue
        keep = 0
        changed = 0
        recorded = m.raw["keys"].get(path, {})
        for qual, old_lits in old_defs.items():
            new_lits = new_defs.get(qual)
            if old_lits == new_lits:
                continue
            if new_lits is None or len(old_lits) != 1 or len(new_lits) != 1 or set(old_lits[0]) != set(new_lits[0]):
                # A qualname defined twice shares one record: do not guess which changed.
                keep = None
                break
            old_lits, new_lits = old_lits[0], new_lits[0]
            for param, value in old_lits.items():
                if new_lits[param] != value:
                    changed += 1
                    keep |= m.bits(recorded.get(qual, {}).get(param))
        if keep is not None and changed:
            narrow_file(c, path, keep, "defaults")


# ------------------------------------------------------------ arg_types values

ARGTYPES_MAP = HERE / "map" / "argtypes.json.gz"


def narrow_by_arg_types(c: Commit) -> None:
    """A file whose only change is the values of entries in one class's arg_types
    literal keeps the tests that validated an instance of an affected class with that
    argument missing. Those values are read only through required_args, which only
    Expression.error_messages consults (map/argtypes.json.gz)."""
    m = _map(ARGTYPES_MAP)
    if m is None:
        return
    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or not c.file_sel.get(path) or c.tag.get(path) != "expr-attr":
            continue
        try:
            change = class_level_change(ast.parse(c.old[path]), ast.parse(new))
        except SyntaxError:
            continue
        if change is None:
            continue
        qual, a, b, cls = change
        name, old = _assign(a)
        name2, new_value = _assign(b)
        if name != "arg_types" or name2 != "arg_types" or not isinstance(old, ast.Dict) or not isinstance(new_value, ast.Dict):
            continue
        if sum(1 for st in cls.body if _assign(st)[0] == "arg_types") != 1 or len(old.keys) != len(new_value.keys):
            continue
        changed = set()
        ok = True
        for ko, kn, vo, vn in zip(old.keys, new_value.keys, old.values, new_value.values):
            if ko is None or kn is None or ast.dump(ko) != ast.dump(kn):
                ok = ok and ko is None and kn is None and ast.dump(vo) == ast.dump(vn)
                continue
            if not isinstance(ko, ast.Constant) or not isinstance(ko.value, str):
                ok = False
            elif ast.dump(vo) != ast.dump(vn):
                if not (isinstance(vo, ast.Constant) and isinstance(vn, ast.Constant)):
                    ok = False
                elif bool(vo.value) != bool(vn.value):
                    changed.add(ko.value)
        if not ok:
            continue
        # The class and every class whose arg_types derive from it.
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        start = f"{module}:{'.'.join(qual)}"
        expr = base.static().expr
        spreads = m.raw.get("spreads", {})
        affected, todo = set(), [start]
        while todo:
            k = todo.pop()
            if k in affected:
                continue
            affected.add(k)
            todo += expr.get(k, [None, []])[1]
            todo += spreads.get(k, [])
        if start not in expr:
            continue  # not an expression class: arg_types means something else here
        keep = 0
        for k in affected:
            rec = m.raw["keys"].get(k, {})
            for arg in changed:
                keep |= m.bits(rec.get(arg))
        narrow_file(c, path, keep, "argtypes")


# ------------------------------------------------------------ fixture parts

FIXTURES_MAP = HERE / "map" / "fixtures.json.gz"


class _LoaderError(Exception):
    pass


def _filter_comments(s: str) -> str:
    # tests/helpers.py at the pinned commit
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _fixture_pairs(text: str):
    """What load_sql_fixture_pairs yields for this text, or _LoaderError if it raises."""
    try:
        statements = _filter_comments(text).split(";")
        out = []
        for i in range(0, len(statements), 2):
            if i + 1 < len(statements):
                sql = statements[i].strip()
                meta = {}
                lines = sql.split("\n")
                j = 0
                while lines[j].startswith("#"):
                    key, val = lines[j].split(":", maxsplit=1)
                    meta[key.lstrip("#").strip()] = val.strip()
                    j += 1
                out.append((meta, "\n".join(lines[j:]), statements[i + 1].strip()))
        return out
    except Exception as error:
        raise _LoaderError() from error


def _fixture_entries(text: str) -> list[str]:
    """What load_sql_fixtures yields for this text."""
    out, lines = [], []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


def _changed_parts(old: str, new: str) -> set[str] | None:
    """The parts of a fixture file (as the helpers yield them) that differ, or None
    when the new text makes a helper raise (every test that loads it fails)."""
    parts = set()
    try:
        a = _fixture_pairs(old)
    except _LoaderError:
        a = None  # no passing test loads it this way at the pinned commit
    if a is not None:
        try:
            b = _fixture_pairs(new)
        except _LoaderError:
            return None
        if len(a) != len(b):
            parts |= {"meta", "sql", "expected"}
        else:
            for x, y in zip(a, b):
                for kind, u, v in zip(("meta", "sql", "expected"), x, y):
                    if u != v:
                        parts.add(kind)
    if _fixture_entries(old) != _fixture_entries(new):
        parts.add("entry")
    return parts


def narrow_by_fixture_reads(c: Commit) -> None:
    """A changed SQL fixture keeps the tests that read a changed part of it (records:
    map/fixtures.json.gz), plus any test that opens it other than through the helpers."""
    raw = _maps.get("fixtures")
    if raw is None:
        raw = _maps["fixtures"] = json.loads(gzip.decompress(FIXTURES_MAP.read_bytes())) if FIXTURES_MAP.exists() else {}
    for path, (old, new) in c.fixtures.items():
        rec = raw.get(path)
        if rec is None or not c.file_sel.get(path):
            continue
        parts = _changed_parts(old, new)
        if parts is None:
            continue
        keep = 0
        for kind in parts | {"direct"}:
            for n in rec.get(kind, []):
                keep |= 1 << n
        narrow_file(c, path, keep, "fixture-parts")


# ------------------------------------------------------------ annotations

USE_ANNOTATIONS = os.environ.get("BR_USE_ANNOTATIONS", "1") == "1"


def _future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(st, ast.ImportFrom) and st.module == "__future__" and any(a.name == "annotations" for a in st.names)
        for st in tree.body
    )


def _strip_function_annotations(tree: ast.Module) -> str:
    """Dump with parameter and return annotations removed, and annotations of local
    variables inside functions. Under `from __future__ import annotations` these are
    never evaluated (locals never are), and nothing in sqlglot or its tests reads a
    function's __annotations__. Class- and module-level annotations stay: dataclasses,
    NamedTuples and TypedDicts read them."""
    import copy

    tree = copy.deepcopy(tree)

    class Strip(ast.NodeTransformer):
        depth = 0

        def _func(self, node):
            for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                a.annotation = None
            for a in (node.args.vararg, node.args.kwarg):
                if a is not None:
                    a.annotation = None
            if not isinstance(node, ast.Lambda):
                node.returns = None
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1
            return node

        visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

        def visit_ClassDef(self, node):
            saved, self.depth = self.depth, 0  # a class body inside a function is class level
            self.generic_visit(node)
            self.depth = saved
            return node

        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            if self.depth:
                node.annotation = ast.Constant(None)
            return node

    return ast.dump(Strip().visit(tree))


def narrow_by_annotations(c: Commit) -> None:
    """A file whose only change is in function annotations keeps no test for that file
    (tests that read the source as data excepted, via narrow_file)."""
    for path, new in c.overlay.items():
        if not c.file_sel.get(path):
            continue
        try:
            old_tree, new_tree = ast.parse(c.old[path]), ast.parse(new)
        except SyntaxError:
            continue
        if not (_future_annotations(old_tree) and _future_annotations(new_tree)):
            continue
        # Cheap gate: every changed line lies in a function header or an annotated
        # assignment, the only places the full comparison below can accept.
        allowed: set[int] = set()
        for node in ast.walk(old_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                allowed.update(range(node.lineno, node.body[0].lineno))
            elif isinstance(node, ast.AnnAssign):
                allowed.update(range(node.lineno, node.end_lineno + 1))
        if not c.changed.get(path) or not c.changed[path] <= allowed:
            continue
        if ast.dump(old_tree) == ast.dump(new_tree):
            continue
        if _strip_function_annotations(old_tree) == _strip_function_annotations(new_tree):
            narrow_file(c, path, 0, "annotations-only")


# ------------------------------------------------------------ TYPE_CHECKING blocks

def _is_type_checking(test) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name) and test.value.id in ("t", "typing")
    )


def narrow_type_checking_flip(c: Commit) -> None:
    """A file whose only change turns a top-level `if TYPE_CHECKING:` holding only imports
    (no else) into `if not TYPE_CHECKING:` now runs those imports at runtime: it binds
    names nothing used before and imports modules earlier. If the patched package still
    imports (more.package_import_failures), that file selects nothing. All 62 such flips
    in the pinned tree were labeled: the 13 that break tests all fail the import check,
    the other 49 break nothing."""
    import more

    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or not c.file_sel.get(path):
            continue
        try:
            a, b = ast.parse(c.old[path]).body, ast.parse(new).body
        except SyntaxError:
            continue
        if len(a) != len(b):
            continue
        diff = [(x, y) for x, y in zip(a, b) if ast.dump(x) != ast.dump(y)]
        if len(diff) != 1:
            continue
        x, y = diff[0]
        if not (isinstance(x, ast.If) and isinstance(y, ast.If) and _is_type_checking(x.test)):
            continue
        if not (isinstance(y.test, ast.UnaryOp) and isinstance(y.test.op, ast.Not)
                and ast.dump(y.test.operand) == ast.dump(x.test)):
            continue
        if x.orelse or y.orelse or ast.dump(ast.Module(x.body, [])) != ast.dump(ast.Module(y.body, [])):
            continue
        if not all(isinstance(st, (ast.Import, ast.ImportFrom)) for st in x.body):
            continue
        if more.package_import_failures(c):
            continue
        narrow_file(c, path, 0, "type-checking-flip")


# ------------------------------------------------------------ TPC-DS entries

USE_TPCDS = os.environ.get("BR_USE_TPCDS", "1") == "1"
TPCDS_MAP = HERE / "map" / "tpcds.json.gz"
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# x86 CPU per recorded (Apple M-series) millisecond, and the fixed cost of a proof
# child (fork, imports, the tables it reads), both measured on a Xeon 4210R.
TPCDS_CPU_FACTOR = 5.0
TPCDS_FIXED_MS = 1500
# Spend at most this share of what dropping the test saves.
TPCDS_SHARE = float(os.environ.get("BR_TPCDS_SHARE", "0.5"))
TPCDS_WALL_BUDGET = float(os.environ.get("BR_TPCDS_WALL", "60"))
_tpcds_wall = [0.0]


TPCDS_STAGES_MAP = HERE / "map" / "tpcds_stages.json.gz"


def prove_tpcds_staged(c: Commit, cov, tests: list[dict], durations: list[int]) -> None:
    """Drop test_execute_tpcds when every changed line runs only in the early stages of
    the TPC-DS entries (parse, the executor's optimize, the DuckDB transpile: never in
    pickling, schema and tables, planning or execution, nor at module level) and, on the
    patched code, those stages produce for every entry that reaches a changed line the
    same output as pinned (tpcds_harness.early_digest). The late stages then receive
    identical input and run identical code."""
    t = next((x["number"] for x in tests if x["id"] == TPCDS_TEST), None)
    if t is None or not (c.sel >> t) & 1 or c.fixtures or not c.overlay or not c.executable:
        return
    if any(not p.startswith("sqlglot/") for p in c.overlay) or c.other_sel:
        return
    raw = _maps.get("tpcds_stages")
    if raw is None:
        raw = _maps["tpcds_stages"] = (json.loads(gzip.decompress(TPCDS_STAGES_MAP.read_bytes()))
                                       if TPCDS_STAGES_MAP.exists() else {})
    if not raw:
        return
    if "late_sets" not in raw:
        raw["late_sets"] = {p: set(ls) for p, ls in raw["late"].items()}
        raw["body_sets"] = {p: set(ls) for p, ls in raw["body"].items()}
    need: set[int] = set()
    for path, lines in c.changed.items():
        late, body = raw["late_sets"].get(path, ()), raw["body_sets"].get(path, ())
        early = raw["early"].get(path, {})
        for line in lines:
            if line in late or line in body:
                return
            entries = early.get(str(line))
            if entries:
                need.update(entries)
            elif (cov.at_line(path, line) >> t) & 1:
                return
    if not need:
        return
    numbers = numbers_of(c.sel)
    saving = split(numbers, durations)[0] - split([n for n in numbers if n != t], durations)[0]
    estimate = TPCDS_FIXED_MS + TPCDS_CPU_FACTOR * sum(raw["early_ms"][e] for e in need)
    if estimate > TPCDS_SHARE * saving or _tpcds_wall[0] > TPCDS_WALL_BUDGET:
        return
    order = sorted(need)
    expected = [raw["early_digest"][e] for e in order]
    start = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 2
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(int(3 * estimate / 1000) + 10)
            sys.path.insert(0, str(SRC))
            from execrun import _OverlayFinder

            sys.meta_path.insert(0, _OverlayFinder(str(SRC), c.overlay))
            import tpcds_harness
            from tests.helpers import load_sql_fixture_pairs

            pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
            tables = tpcds_harness.load_tables()
            code = 0
            for e, digest in zip(order, expected):
                if tpcds_harness.early_digest(pairs[e], tables) != digest:
                    code = 1
                    break
        except BaseException:  # noqa: BLE001 - any failure means no proof
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    _tpcds_wall[0] += time.monotonic() - start
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    c.note += f" tpcds-staged:{len(order)}entries:{'same' if ok else 'differs'}"
    if ok:
        c.sel &= ~(1 << t)


TPCDS_CODEGEN_MAP = HERE / "map" / "tpcds_codegen.json.gz"


def _late_mentions(raw, name: str) -> bool:
    """Whether any execution-time line of any entry mentions the name (word match)."""
    if "late_text" not in raw:
        chunks = []
        for p, ls in raw["late"].items():
            try:
                src = (SRC / p).read_text(encoding="utf-8").split("\n")
            except OSError:
                return True
            chunks.extend(src[l - 1] for l in ls if l <= len(src))
        raw["late_text"] = "\n".join(chunks)
    return __import__("re").search(rf"\b{__import__('re').escape(name)}\b", raw["late_text"]) is not None


def prove_tpcds_codegen(c: Commit, cov, tests: list[dict], durations: list[int]) -> None:
    """Like prove_tpcds_staged, one stage further: the early stages also cover planning
    and the Python source PythonGenerator emits for every step (tpcds_harness.
    codegen_digest), so only the executor's runtime runs on what they produce. Changed
    lines must not run at execution time. A class-level constant may change if no
    execution-time line mentions its name; class bodies run at import, so every entry
    is then re-digested."""
    t = next((x["number"] for x in tests if x["id"] == TPCDS_TEST), None)
    if t is None or not (c.sel >> t) & 1 or c.fixtures or not c.overlay or not c.executable:
        return
    if any(not p.startswith("sqlglot/") for p in c.overlay) or c.other_sel:
        return
    raw = _maps.get("tpcds_codegen")
    if raw is None:
        raw = _maps["tpcds_codegen"] = (json.loads(gzip.decompress(TPCDS_CODEGEN_MAP.read_bytes()))
                                        if TPCDS_CODEGEN_MAP.exists() else {})
    if not raw:
        return
    if "late_sets" not in raw:
        raw["late_sets"] = {p: set(ls) for p, ls in raw["late"].items()}
        raw["body_sets"] = {p: set(ls) for p, ls in raw["body"].items()}
    n_entries = len(raw["early_digest"])
    need: set[int] = set()
    body_paths = []
    # Cheap set lookups first; parsing for the class-constant case only once it can pay.
    for path, lines in c.changed.items():
        late, body = raw["late_sets"].get(path, ()), raw["body_sets"].get(path, ())
        early = raw["early"].get(path, {})
        for line in lines:
            if line in late:
                return
            if line in body:
                body_paths.append(path)
                need.update(range(n_entries))  # class bodies run at import for every entry
                continue
            entries = early.get(str(line))
            if entries:
                need.update(entries)
            elif (cov.at_line(path, line) >> t) & 1:
                return
    if not need:
        return
    numbers = numbers_of(c.sel)
    saving = split(numbers, durations)[0] - split([n for n in numbers if n != t], durations)[0]
    estimate = TPCDS_FIXED_MS + TPCDS_CPU_FACTOR * sum(raw["early_ms"][e] for e in need)
    if estimate > TPCDS_SHARE * saving or _tpcds_wall[0] > TPCDS_WALL_BUDGET:
        return
    for path in set(body_paths):
        # A module- or class-level line may change only as one class-level constant
        # whose name no execution-time line mentions.
        try:
            change = class_level_change(ast.parse(c.old[path]), ast.parse(c.overlay[path]))
        except SyntaxError:
            return
        if change is None:
            return
        n1, _ = _assign(change[1])
        n2, _ = _assign(change[2])
        if not n1 or n1 != n2 or _late_mentions(raw, n1):
            return
    order = sorted(need)
    expected = [raw["early_digest"][e] for e in order]
    start = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 2
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(int(3 * estimate / 1000) + 10)
            sys.path.insert(0, str(SRC))
            from execrun import _OverlayFinder

            sys.meta_path.insert(0, _OverlayFinder(str(SRC), c.overlay))
            import tpcds_harness
            from tests.helpers import load_sql_fixture_pairs

            pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
            tables = tpcds_harness.load_tables()
            code = 0
            for e, digest in zip(order, expected):
                if tpcds_harness.codegen_digest(pairs[e], tables) != digest:
                    code = 1
                    break
        except BaseException:  # noqa: BLE001 - any failure means no proof
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    _tpcds_wall[0] += time.monotonic() - start
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    c.note += f" tpcds-codegen:{len(order)}entries:{'same' if ok else 'differs'}"
    if ok:
        c.sel &= ~(1 << t)


def prove_tpcds(c: Commit, cov, tests: list[dict], durations: list[int]) -> None:
    """Drop test_execute_tpcds when re-running, on the patched code, every TPC-DS entry
    whose own run reached a changed line reproduces that entry's pinned digest
    (tpcds_harness.run_entry: parse, the pool's pickle round trip, execute, and the
    DuckDB transpile). Entries that reach no changed line run exactly as pinned."""
    t = next((x["number"] for x in tests if x["id"] == TPCDS_TEST), None)
    if t is None or not (c.sel >> t) & 1 or c.fixtures or not c.overlay or not c.executable:
        return
    if any(not p.startswith("sqlglot/") for p in c.overlay) or c.other_sel:
        return
    raw = _maps.get("tpcds")
    if raw is None:
        raw = _maps["tpcds"] = json.loads(gzip.decompress(TPCDS_MAP.read_bytes())) if TPCDS_MAP.exists() else {}
    if not raw:
        return
    need: set[int] = set()
    for path, lines in c.changed.items():
        by_line = raw["lines"].get(path, {})
        body = set(raw["body"].get(path, ()))
        for line in lines:
            if line in body:
                return  # module or class level: runs at import for every entry
            entries = by_line.get(str(line))
            if entries:
                need.update(entries)
            elif (cov.at_line(path, line) >> t) & 1:
                return  # the test runs this line outside the entries' work
    if not need:
        return
    numbers = numbers_of(c.sel)
    saving = split(numbers, durations)[0] - split([n for n in numbers if n != t], durations)[0]
    estimate = TPCDS_FIXED_MS + TPCDS_CPU_FACTOR * sum(raw["entries"][e][2] for e in need)
    if estimate > TPCDS_SHARE * saving or _tpcds_wall[0] > TPCDS_WALL_BUDGET:
        return
    order = sorted(need, key=lambda e: raw["entries"][e][2])
    expected = [raw["entries"][e][1] for e in order]
    start = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 2
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(int(3 * estimate / 1000) + 10)
            sys.path.insert(0, str(SRC))
            from execrun import _OverlayFinder

            sys.meta_path.insert(0, _OverlayFinder(str(SRC), c.overlay))
            import tpcds_harness
            from tests.helpers import load_sql_fixture_pairs

            pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
            tables = tpcds_harness.load_tables() if any(pairs[e][0].get("execute") for e in order) else None
            code = 0
            for e, digest in zip(order, expected):
                if tpcds_harness.run_entry(pairs[e], tables) != digest:
                    code = 1
                    break
        except BaseException:  # noqa: BLE001 - any failure means no proof
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    _tpcds_wall[0] += time.monotonic() - start
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    c.note += f" tpcds-proof:{len(order)}entries:{'same' if ok else 'differs'}"
    if ok:
        c.sel &= ~(1 << t)


# ------------------------------------------------------------ generator memory

GENMEM_MAP = HERE / "map" / "genmem.json.gz"
GENMEM_TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"


def prove_genmem(c: Commit, cov, tests: list[dict], durations: list[int]) -> None:
    """Drop test_generator_memory when its subtests for every dialect whose own run
    reached a changed line still pass on the patched code (genmem_harness.run_dialect:
    load the dialect, build its generator 1,100 times, memory must stay flat). The
    patched Dialects enum must list the same dialects; the others run as pinned."""
    t = next((x["number"] for x in tests if x["id"] == GENMEM_TEST), None)
    if t is None or not (c.sel >> t) & 1 or c.fixtures or not c.overlay or not c.executable:
        return
    if any(not p.startswith("sqlglot/") for p in c.overlay) or c.other_sel:
        return
    raw = _maps.get("genmem")
    if raw is None:
        raw = _maps["genmem"] = json.loads(gzip.decompress(GENMEM_MAP.read_bytes())) if GENMEM_MAP.exists() else {}
    if not raw:
        return
    need: set[int] = set()
    for path, lines in c.changed.items():
        by_line = raw["lines"].get(path, {})
        for line in lines:
            entries = by_line.get(str(line))
            if entries:
                need.update(entries)
            elif (cov.at_line(path, line) >> t) & 1:
                return  # the test runs this line outside the dialect loop
    if not need:
        return
    numbers = numbers_of(c.sel)
    saving = split(numbers, durations)[0] - split([n for n in numbers if n != t], durations)[0]
    estimate = TPCDS_FIXED_MS + TPCDS_CPU_FACTOR * sum(raw["ms"][i] for i in need)
    if estimate > TPCDS_SHARE * saving or _tpcds_wall[0] > TPCDS_WALL_BUDGET:
        return
    names = list(raw["names"])
    order = [names[i] for i in sorted(need)]
    start = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 2
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(int(3 * estimate / 1000) + 10)
            sys.path.insert(0, str(SRC))
            from execrun import _OverlayFinder

            sys.meta_path.insert(0, _OverlayFinder(str(SRC), c.overlay))
            import genmem_harness

            if genmem_harness.dialect_names() != names:
                code = 3
            else:
                code = 0
                for name in order:
                    if not genmem_harness.run_dialect(name):
                        code = 1
                        break
        except BaseException:  # noqa: BLE001 - any failure means no proof
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    _tpcds_wall[0] += time.monotonic() - start
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    c.note += f" genmem-proof:{len(order)}dialects:{'pass' if ok else 'no'}"
    if ok:
        c.sel &= ~(1 << t)


# --------------------------------------------- optimizer fixture-entry proof

USE_OPTFIX = os.environ.get("BR_USE_OPTFIX", "0") == "1"
OPTFIX_MAP = HERE / "map" / "optfix.json.gz"
OPTFIX_CLASS = "tests/test_optimizer.py::TestOptimizer::"
OPTFIX_FIXED_MS = int(os.environ.get("BR_OPTFIX_FIXED_MS", "500"))
OPTFIX_SHARE = float(os.environ.get("BR_OPTFIX_SHARE", str(TPCDS_SHARE)))


def _stub_duckdb_pandas() -> None:
    """Stand-ins for the two third-party modules tests/test_optimizer.py imports. They
    can only make a test fail: setUpClass may connect and run statements, but reading
    any result and assert_frame_equal raise, so an entry that executes SQL in DuckDB
    fails its subtest and its test is not dropped."""
    import types

    class _Result:
        def __getattr__(self, name):
            raise RuntimeError("duckdb is not available in the proof")

    class _Connection:
        def execute(self, *args, **kwargs):
            return _Result()

        def __getattr__(self, name):
            raise RuntimeError("duckdb is not available in the proof")

    def connect(*args, **kwargs):
        return _Connection()

    def assert_frame_equal(*args, **kwargs):
        raise AssertionError("pandas is not available in the proof")

    duckdb = types.ModuleType("duckdb")
    duckdb.connect = connect
    pandas = types.ModuleType("pandas")
    testing = types.ModuleType("pandas.testing")
    testing.assert_frame_equal = assert_frame_equal
    pandas.testing = testing
    sys.modules["duckdb"] = duckdb
    sys.modules["pandas"] = pandas
    sys.modules["pandas.testing"] = testing


def prove_optfix(c: Commit, cov, tests: list[dict], durations: list[int]) -> None:
    """Drop fixture-driven tests of tests/test_optimizer.py (check_file and the
    annotate loops) whose entries pass on the patched code. map/optfix.json.gz holds
    each entry's own coverage (the real test run with the fixture loader yielding that
    entry alone). A test qualifies when every changed line it reaches is reached only
    inside entries: never while importing, in the test's run with no entries, or at
    module or class level. The real test then runs on the patched code with the loader
    yielding just the entries that reach a changed line; the others run exactly as
    pinned, where the test passes."""
    if not USE_OPTFIX or not c.sel or c.fixtures or not c.overlay or not c.executable:
        return
    if any(not p.startswith("sqlglot/") for p in c.overlay) or c.other_sel:
        return
    raw = _maps.get("optfix")
    if raw is None:
        raw = _maps["optfix"] = json.loads(gzip.decompress(OPTFIX_MAP.read_bytes())) if OPTFIX_MAP.exists() else {}
    if not raw:
        return
    number = {x["id"]: x["number"] for x in tests}
    imported = {p: set(v) for p, v in raw["import"].items()}
    imported_body = {p: set(v) for p, v in raw["import_body"].items()}
    cands = []
    for name, t in raw["tests"].items():
        n = number.get(OPTFIX_CLASS + name)
        if n is None or not (c.sel >> n) & 1 or not t["ok"]:
            continue
        need: set[int] = set()
        ok = True
        for path, lines in c.changed.items():
            outside = (imported.get(path, set()) | imported_body.get(path, set())
                       | set(t["body"].get(path, ())) | set(t["skeleton"].get(path, ())))
            by_line = t["lines"].get(path, {})
            for line in lines:
                if line in outside:
                    ok = False
                    break
                entries = by_line.get(str(line))
                if entries:
                    need.update(entries)
                elif (cov.at_line(path, line) >> n) & 1:
                    ok = False  # the test runs this line somewhere the entry traces did not see
                    break
            if not ok:
                break
        if ok and need:
            cost = sum(t["entries"][e][2] for e in need)
            cands.append((cost, n, name, sorted(need)))
    if not cands:
        return
    cands.sort()
    numbers = numbers_of(c.sel)
    drop = {n for _, n, _, _ in cands}
    saving = split(numbers, durations)[0] - split([x for x in numbers if x not in drop], durations)[0]
    estimate = OPTFIX_FIXED_MS + TPCDS_CPU_FACTOR * sum(cost for cost, _, _, _ in cands)
    if estimate > OPTFIX_SHARE * saving or _tpcds_wall[0] > TPCDS_WALL_BUDGET:
        return
    keep = {name: [tuple(raw["tests"][name]["entries"][e][:2]) for e in need] for _, _, name, need in cands}
    start = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            signal.alarm(int(3 * estimate / 1000) + 10)
            import gc
            import unittest

            gc.freeze()  # the selector's own objects: never garbage, never worth scanning
            gc.enable()
            os.environ.setdefault("SKIP_INTEGRATION", "0")
            os.chdir(str(SRC))
            sys.path.insert(0, str(SRC))
            sys.dont_write_bytecode = True
            from execrun import SyncPool, _OverlayFinder

            sys.meta_path.insert(0, _OverlayFinder(str(SRC), c.overlay))
            _stub_duckdb_pandas()
            import tests.test_optimizer as T

            T.ProcessPoolExecutor = SyncPool
            original = T.load_sql_fixture_pairs
            wanted: set = set()

            def filtered(filename):
                for i, item in enumerate(original(filename)):
                    if (filename, i) in wanted:
                        yield item

            T.load_sql_fixture_pairs = filtered
            for _, n, name, _ in cands:
                wanted.clear()
                wanted.update(keep[name])
                res = unittest.TestResult()
                unittest.TestSuite([T.TestOptimizer(name)]).run(res)
                if res.wasSuccessful() and res.testsRun == 1 and not res.skipped:
                    os.write(w, f"{n}\n".encode())
        except BaseException:  # noqa: BLE001 - whatever ran no further proves nothing more
            pass
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    _, _, usage = os.wait4(pid, 0)
    _tpcds_wall[0] += time.monotonic() - start
    passed = {int(x) for x in data.split()}
    c.note += (f" optfix:{len(cands)}tests:{len(passed)}pass:est{estimate:.0f}"
               f":cpu{(usage.ru_utime + usage.ru_stime) * 1000:.0f}")
    for n in passed:
        c.sel &= ~(1 << n)


# ------------------------------------------------------------------- execution

USE_WARM = os.environ.get("BR_USE_WARM", "0") == "1"
STOP_ON_FAIL = os.environ.get("BR_STOP_ON_FAIL", "1") == "1"
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
_THIRD_PARTY = ("numpy", "pandas", "duckdb")


class Job:
    """A commit's candidates, to be run against the patched code."""

    def __init__(self, jid: int, c: Commit, numbers: list[int], tests: list[dict], deadline_s: float):
        self.id = jid
        self.c = c
        self.numbers = numbers
        self.spec = []
        for n in numbers:
            file, cls, meth = tests[n]["id"].split("::")
            self.spec.append((n, file, cls, meth))
        self.deadline_s = deadline_s
        self.deadline = 0.0
        self.results: dict[int, bool] = {}
        self.done = False       # every test ran
        self.finished = False   # the worker is gone
        self.pid = 0
        self.fd = -1            # cold runs only
        self.buf = b""
        self.mode = ""

    def line(self, words: list[bytes]) -> None:
        if words[0] == b"done":
            self.done = True
        elif words[0] == b"T":
            self.c.note += " " + words[1].decode()
        else:
            n = int(words[0])
            self.results[n] = self.results.get(n, False) or words[1] == b"1"

    def kill(self) -> None:
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass

    def keep(self) -> list[int]:
        """Candidates to keep: the ones that failed, plus any that did not finish."""
        if self.done:
            return [n for n in self.numbers if self.results.get(n, False)]
        return [n for n in self.numbers if self.results.get(n, True)]

    # -- cold: a child forked from this process, which has never imported sqlglot

    def start_cold(self) -> None:
        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            try:
                import execrun

                os.close(r)
                os.setsid()
                execrun.child_main(str(SRC), self.c.overlay, self.spec, w, STOP_ON_FAIL)
            finally:
                os._exit(1)
        os.close(w)
        self.pid, self.fd, self.mode = pid, r, "cold"
        self.deadline = time.monotonic() + self.deadline_s

    def feed_cold(self) -> None:
        data = os.read(self.fd, 65536)
        if not data:
            self.end_cold()
            return
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            self.line(line.split())

    def end_cold(self) -> None:
        if self.finished:
            return
        self.finished = True
        os.close(self.fd)
        self.kill()
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


class Warm:
    """The warm process (warm.Zygote) and the pipes to it."""

    def __init__(self):
        import warm

        job_r, job_w = os.pipe()
        out_r, out_w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(job_w)
                os.close(out_r)
                warm.Zygote(str(SRC), job_r, out_w).serve()
            finally:
                os._exit(1)
        os.close(job_r)
        os.close(out_w)
        self.pid, self.job_w, self.fd = pid, job_w, out_r
        self.buf = b""

    def submit(self, job: Job) -> None:
        import pickle
        import struct

        data = pickle.dumps({"id": job.id, "overlay": job.c.overlay, "tests": job.spec, "stop_on_fail": STOP_ON_FAIL})
        os.write(self.job_w, struct.pack("<I", len(data)) + data)
        job.mode = "warm?"

    def lines(self) -> list[list[bytes]]:
        data = os.read(self.fd, 65536)
        if not data:
            return [[b"eof"]]
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        return [line.split() for line in lines]

    def close(self) -> None:
        try:
            os.close(self.job_w)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        os.close(self.fd)


def rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, ValueError, IndexError):
        return 0


def preload_third_party(jobs: list[Job]) -> None:
    """Import numpy/pandas/duckdb once here if a test file to be run imports them, so
    every forked child shares them."""
    import re

    files = {t[1] for j in jobs for t in j.spec}
    wanted = set()
    for f in files:
        try:
            text = (SRC / f).read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r"^(?:import|from)\s+(\w+)", text, re.M):
            if m.group(1) in _THIRD_PARTY:
                wanted.add(m.group(1))
    for name in _THIRD_PARTY:
        if name in wanted:
            try:
                __import__(name)
            except Exception:
                pass
    if wanted:
        try:
            import unittest.mock  # noqa: F401
            if "pandas" in wanted:
                import pandas.testing  # noqa: F401
        except Exception:
            pass


def run_all(jobs: list[Job], start: float) -> None:
    """Run every job, at most PARALLEL at a time. Results stay on the jobs; a job that
    never ran keeps mode ""."""
    if not jobs:
        return
    preload_third_party(jobs)
    warm = None
    if USE_WARM:
        try:
            warm = Warm()
        except Exception as error:
            log(f"warm process failed to start ({error!r})")
    by_id = {j.id: j for j in jobs}
    queue = list(jobs)
    active: list[Job] = []
    while queue or active:
        while queue and len(active) < PARALLEL:
            j = queue.pop(0)
            if time.monotonic() - start > WALL_BUDGET:
                j.c.note += " exec-skipped(wall)"
                continue
            if warm is not None:
                warm.submit(j)
                j.deadline = time.monotonic() + j.deadline_s + 20
            else:
                j.start_cold()
            active.append(j)
        if not active:
            break
        fds = [j.fd for j in active if j.mode == "cold"]
        if warm is not None:
            fds.append(warm.fd)
        ready, _, _ = select.select(fds, [], [], 0.05)
        if warm is not None and warm.fd in ready:
            for words in warm.lines():
                if words[0] == b"eof":
                    # The warm process died: rerun its jobs cold.
                    for j in active:
                        if j.mode.startswith("warm") and not j.finished:
                            j.kill()
                            j.results.clear()
                            j.done = False
                            j.start_cold()
                    warm.close()
                    warm = None
                    break
                j = by_id.get(int(words[0]))
                if j is None or j.mode == "cold":
                    continue
                if words[1] == b"cold":
                    j.start_cold()
                elif words[1] == b"pid":
                    j.pid = int(words[2])
                    j.mode = "warm"
                    j.c.note += " " + b" ".join(words[3:]).decode()
                    j.deadline = time.monotonic() + j.deadline_s
                elif words[1] == b"exit":
                    j.finished = True
                else:
                    j.line(words[1:])
        now = time.monotonic()
        for j in list(active):
            if j.mode == "cold":
                if j.fd in ready:
                    j.feed_cold()
                if not j.finished and (now > j.deadline or rss_kb(j.pid) > 1_200_000):
                    j.c.note += " exec-killed"
                    j.end_cold()
            elif not j.finished and (now > j.deadline or (j.pid and rss_kb(j.pid) > 1_200_000)):
                j.c.note += " exec-killed"
                j.kill()
                if not j.pid:
                    j.finished = True
            if j.finished:
                active.remove(j)
                j.c.note += f" t={time.monotonic() - start:.2f}"
                j.c.note += f" exec[{j.mode}]:{len(j.numbers)}->{len(j.keep())}{'' if j.done else '(partial)'}"
    if warm is not None:
        warm.close()


# ----------------------------------------------------------------------- split


def split(numbers: list[int], durations: list[int]) -> tuple[int, list[list[int]]]:
    """Longest-processing-time packing for each worker count; returns (cost, workers)."""
    if not numbers:
        return 0, []
    order = sorted(numbers, key=lambda n: -durations[n])
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            bins[j].append(n)
            loads[j] += durations[n]
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best


# ------------------------------------------------------------------------ main


def numbers_of(bits: int) -> list[int]:
    out = []
    n = 0
    while bits:
        low = bits & -bits
        out.append(low.bit_length() - 1)
        bits ^= low
    return out


def main() -> None:
    # A short run that only allocates: cycle collection passes over the parsed trees cost
    # more than they free. Children that run tests turn it back on.
    import gc

    gc.disable()
    start = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = [0] * len(tests)
    for t in tests:
        durations[t["number"]] = int(t["seconds"] * 1000 + 0.5)
    no_exec = {t["number"] for t in tests if t["id"] in NO_EXEC_IDS}
    cov = base.CoverageMap(base.MAP)
    valid = (1 << len(tests)) - 1
    if not (SRC / "sqlglot" / "parser.py").exists():
        # Without the pinned source nothing can be analyzed: run everything.
        log(f"pinned source not found under {SRC}; selecting every test")
        for patch in sorted((DATA / "commits").glob("*.patch")):
            print(json.dumps({"commit": patch.stem, "workers": split(list(range(len(tests))), durations)[1]}), flush=True)
        return
    cmp_map = None
    commits: list[Commit] = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        c = analyze(patch.stem, text, cov, valid)
        if USE_CMP and c.sel and c.file_sel and (CMP_MAP.exists() or REL_MAP.exists()):
            try:
                if cmp_map is None:
                    cmp_map = RelMap(REL_MAP) if REL_MAP.exists() else CmpMap(CMP_MAP)
                narrow_by_comparisons(c, cmp_map)
            except Exception as error:
                log(f"{c.id}: comparison narrowing failed ({error!r})")
        if (USE_ATTR or USE_DICT) and c.file_sel:
            saved = (c.sel, dict(c.file_sel))
            try:
                import more

                more.narrow_class_level(c, cov)
                more.narrow_module_level(c, cov)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: read narrowing failed ({error!r})")
        if USE_DEFAULTS and c.sel and c.file_sel:
            saved = (c.sel, dict(c.file_sel))
            try:
                narrow_by_defaults(c)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: default narrowing failed ({error!r})")
        if USE_DEFAULTS and c.sel and c.file_sel:
            saved = (c.sel, dict(c.file_sel))
            try:
                narrow_by_arg_types(c)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: arg_types narrowing failed ({error!r})")
        if c.sel and c.file_sel:
            saved = (c.sel, dict(c.file_sel))
            try:
                narrow_type_checking_flip(c)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: TYPE_CHECKING narrowing failed ({error!r})")
        if USE_ANNOTATIONS and c.sel and c.file_sel:
            saved = (c.sel, dict(c.file_sel))
            try:
                narrow_by_annotations(c)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: annotation narrowing failed ({error!r})")
        if USE_FIXTURES and c.sel and c.fixtures:
            saved = (c.sel, dict(c.file_sel))
            try:
                narrow_by_fixture_reads(c)
            except Exception as error:
                c.sel, c.file_sel = saved
                log(f"{c.id}: fixture narrowing failed ({error!r})")
        commits.append(c)

    if USE_TPCDS:
        for c in commits:
            try:
                if os.environ.get("BR_USE_TPCDS_STAGED", "1") == "1":
                    prove_tpcds_staged(c, cov, tests, durations)
                if "tpcds-staged" not in c.note and os.environ.get("BR_USE_TPCDS_CODEGEN", "1") == "1":
                    prove_tpcds_codegen(c, cov, tests, durations)
            except Exception as error:
                log(f"{c.id}: staged tpcds proof failed ({error!r})")
            try:
                prove_tpcds(c, cov, tests, durations)
            except Exception as error:
                log(f"{c.id}: tpcds proof failed ({error!r})")
            try:
                prove_genmem(c, cov, tests, durations)
            except Exception as error:
                log(f"{c.id}: generator memory proof failed ({error!r})")
            try:
                prove_optfix(c, cov, tests, durations)
            except Exception as error:
                log(f"{c.id}: optimizer fixture proof failed ({error!r})")

    final: dict[str, list[int]] = {}
    jobs: list[Job] = []
    for c in commits:
        numbers = numbers_of(c.sel)
        final[c.id] = numbers
        if not USE_EXEC or not numbers or not c.executable or not c.overlay:
            continue
        if any(not p.startswith("sqlglot/") for p in c.overlay):
            continue
        if any(n in no_exec for n in numbers):
            continue
        total = sum(durations[n] for n in numbers)
        estimate = EXEC_OVERHEAD_MS + total
        if any("::TestExecutor::" in tests[n]["id"] for n in numbers):
            estimate += EXECUTOR_SETUP_MS
        # Running costs about the candidates' recorded time; it pays when they would all
        # pass, which frees the whole selection. EXEC_SHARE is the share of the static cost
        # we are willing to spend on finding out.
        if estimate > EXEC_LIMIT_MS or estimate > EXEC_SHARE * split(numbers, durations)[0]:
            continue
        jobs.append(Job(len(jobs), c, numbers, tests, 10.0 + 5.0 * total / 1000))
    run_all(jobs, start)
    for j in jobs:
        if j.mode:
            final[j.c.id] = j.keep()

    for c in commits:
        cost, workers = split(final[c.id], durations)
        print(json.dumps({"commit": c.id, "workers": workers}), flush=True)
        log(f"{c.id}: {len(final[c.id])} tests, {len(workers)} workers; {', '.join(c.kinds)};{c.note}")
    # Skip interpreter teardown: freeing the maps and trees one by one is pure cost.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
