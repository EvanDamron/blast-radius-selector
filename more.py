"""Further narrowing stages on top of selector.py (same Commit objects).

narrow_class_level   one class-level assignment changed or deleted: dict-key and
                     attribute read records, guarded by an import check when the new
                     value runs code at import.
narrow_module_level  one module-level definition deleted or renamed: the tests that
                     execute a reference to the name (map/modrefs.json.gz).
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import selector as S

MODREFS = Path(os.environ.get("BR_MODREFS", str(S.HERE / "map" / "modrefs.json.gz")))
USE_IMPORT_CHECK = os.environ.get("BR_USE_IMPORT_CHECK", "1") == "1"
_DUMP = ast.dump


# ------------------------------------------------------------- single-edit diff


def single_edit(old_tree: ast.Module, new_tree: ast.Module, old_text: str = "", new_text: str = ""):
    """(class path, old statement or None, new statement or None, class node or None) when
    the modules differ by exactly one statement changed or deleted, at module level or in
    a class body (never inside a function), else None.

    With the source texts, statements whose lines are identical are equal without
    comparing their trees."""
    found = []
    old_lines = old_text.split("\n") if old_text else None
    new_lines = new_text.split("\n") if new_text else None

    def same(a, b) -> bool:
        if type(a) is not type(b):
            return False
        if old_lines is not None and old_lines[a.lineno - 1 : a.end_lineno] == new_lines[b.lineno - 1 : b.end_lineno]:
            first = getattr(a, "decorator_list", None)
            if not first and a.col_offset == b.col_offset:
                return True
        return _DUMP(a) == _DUMP(b)

    def walk(a_body, b_body, qual, owner) -> bool:
        if len(a_body) == len(b_body) + 1:
            # one statement deleted: everything else must line up
            i = 0
            while i < len(b_body) and same(a_body[i], b_body[i]):
                i += 1
            for x, y in zip(a_body[i + 1 :], b_body[i:]):
                if not same(x, y):
                    return False
            found.append((qual, a_body[i], None, owner))
            return len(found) == 1
        if len(a_body) != len(b_body):
            return False
        for a, b in zip(a_body, b_body):
            if type(a) is not type(b):
                return False
            if same(a, b):
                continue
            if isinstance(a, ast.ClassDef):
                if a.name != b.name or _DUMP(ast.Tuple(elts=a.bases + a.decorator_list + [k.value for k in a.keywords])) != _DUMP(
                    ast.Tuple(elts=b.bases + b.decorator_list + [k.value for k in b.keywords])
                ):
                    return False
                if not walk(a.body, b.body, qual + [a.name], a):
                    return False
            else:
                found.append((qual, a, b, owner))
                if len(found) > 1:
                    return False
        return True

    if not walk(old_tree.body, new_tree.body, [], None) or len(found) != 1:
        return None
    return found[0]


_edit_cache: dict[tuple[str, str], object] = {}


def edit_of(c, path: str):
    """single_edit for one changed file of a commit, computed once."""
    key = (c.id, path)
    if key not in _edit_cache:
        try:
            old_tree, new_tree = ast.parse(c.old[path]), ast.parse(c.overlay[path])
            _edit_cache[key] = (single_edit(old_tree, new_tree, c.old[path], c.overlay[path]), old_tree)
        except SyntaxError:
            _edit_cache[key] = (None, None)
    return _edit_cache[key]


# ---------------------------------------------------------------- import safety


def _literal_only(node) -> bool:
    return all(
        isinstance(n, (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Load, ast.UnaryOp, ast.USub, ast.UAdd))
        for n in ast.walk(node)
    )


def import_safe(a, b) -> bool:
    """True when replacing expression a by b cannot change what happens while the module
    is imported, beyond the value itself: only literals differ, entries of a display were
    dropped, or the difference is inside a lambda body (which runs later)."""
    if type(a) is not type(b):
        return _literal_only(a) and _literal_only(b)
    if isinstance(a, ast.Constant):
        return True
    if isinstance(a, ast.Lambda):
        return _DUMP(a.args) == _DUMP(b.args)
    if isinstance(a, ast.Dict):
        old = [(None if k is None else _DUMP(k), v) for k, v in zip(a.keys, a.values)]
        new = [(None if k is None else _DUMP(k), v) for k, v in zip(b.keys, b.values)]
        new_keys = {k for k, _ in new if k is not None}
        old_by_key = {k: v for k, v in old if k is not None}
        if len(old_by_key) != sum(1 for k, _ in old if k is not None):
            return False
        if [_DUMP(v) for k, v in old if k is None] != [_DUMP(v) for k, v in new if k is None]:
            return False
        for (k, v), node_k in zip(new, b.keys):
            if k is None:
                continue
            if k in old_by_key:
                if not import_safe(old_by_key[k], v):
                    return False
            elif not (_literal_only(node_k) and _literal_only(v)):
                return False
        return True
    if isinstance(a, (ast.List, ast.Tuple, ast.Set)):
        if len(a.elts) == len(b.elts):
            return all(import_safe(x, y) for x, y in zip(a.elts, b.elts))
        old = [_DUMP(x) for x in a.elts]
        it = iter(old)
        return all(any(x == y for y in it) for x in (_DUMP(e) for e in b.elts))  # b is a subsequence of a
    if isinstance(a, ast.UnaryOp) and isinstance(a.operand, ast.Constant):
        return type(a.op) is type(b.op) and isinstance(b.operand, ast.Constant)
    return _DUMP(a) == _DUMP(b)


_import_ok: dict[tuple[str, str], bool] = {}


def module_imports(c, path: str) -> bool:
    """Import the patched module in a child (the patch applied in memory). False when it
    raises: then every test that imports it fails."""
    key = (c.id, path)
    if key in _import_ok:
        return _import_ok[key]
    name = path[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            import execrun

            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.chdir(str(S.SRC))
            sys.path.insert(0, str(S.SRC))
            sys.meta_path.insert(0, execrun._OverlayFinder(str(S.SRC), c.overlay))
            sys.dont_write_bytecode = True
            importlib.import_module("sqlglot")
            importlib.import_module(name)
            os._exit(0)
        except BaseException:  # noqa: BLE001
            os._exit(1)
    ok = os.waitpid(pid, 0)[1] == 0
    _import_ok[key] = ok
    return ok


def _import_all(overlay: dict[str, str]) -> set[str]:
    """In a child: import every sqlglot module with the patch applied; returns the files of
    the modules that raise."""
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            import pkgutil

            import execrun

            os.close(r)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.chdir(str(S.SRC))
            sys.path.insert(0, str(S.SRC))
            if overlay:
                sys.meta_path.insert(0, execrun._OverlayFinder(str(S.SRC), overlay))
            sys.dont_write_bytecode = True
            failed = []
            try:
                pkg = importlib.import_module("sqlglot")
                # a package that fails to import is reported through onerror, not raised
                for m in pkgutil.walk_packages(pkg.__path__, "sqlglot.", onerror=failed.append):
                    if m.name.endswith("__main__"):
                        continue
                    try:
                        importlib.import_module(m.name)
                    except BaseException:  # noqa: BLE001
                        if m.name not in failed:
                            failed.append(m.name)
            except BaseException:  # noqa: BLE001
                failed.append("sqlglot")
            os.write(w, "\n".join(failed).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    status = os.waitpid(pid, 0)[1]
    names = [x for x in data.decode().split("\n") if x]
    if status != 0 and not names:
        names = ["sqlglot"]
    out = set()
    for name in names:
        rel = name.replace(".", "/")
        out.add(rel + "/__init__.py" if (S.SRC / rel / "__init__.py").exists() else rel + ".py")
    return out


_baseline_failures: set[str] | None = None
_pkg_failures: dict[str, set[str]] = {}


def package_import_failures(c) -> set[str]:
    """Files of the sqlglot modules that import at the pinned commit but not with this
    patch. Every test that imports one of them fails."""
    global _baseline_failures
    if c.id not in _pkg_failures:
        failed = _import_all(c.overlay)
        if failed:
            if _baseline_failures is None:
                _baseline_failures = _import_all({})
            failed -= _baseline_failures
        _pkg_failures[c.id] = failed
    return _pkg_failures[c.id]


def widen_for_import_failures(c, path: str, cov) -> bool:
    failed = package_import_failures(c)
    if not failed:
        return False
    valid = (1 << cov.n) - 1
    add = 0
    for f in failed:
        add |= cov.importers(f) & valid
    c.file_sel[path] = c.file_sel.get(path, 0) | add
    total = c.other_sel
    for b in c.file_sel.values():
        total |= b
    c.sel = total
    c.note += f" widened({len(failed)} modules no longer import):{c.sel.bit_count()}"
    return True


def _is_special(path: str, qual: list[str]) -> bool:
    m = S._map(S.ATTR_MAP)
    if m is None:
        return False
    if not hasattr(m, "special"):
        m.strings, m.special, m.classes = set(m.raw["strings"]), set(m.raw["special"]), set(m.raw["classes"])
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return f"{module}:{'.'.join(qual)}" in m.special


def widen_to_importers(c, path: str, cov, why: str = "import-fails") -> None:
    """Every test that imports the module is selected (the patched module does not
    import, or the edit can be observed without naming what changed)."""
    c.file_sel[path] = c.file_sel.get(path, 0) | (cov.importers(path) & ((1 << cov.n) - 1))
    total = c.other_sel
    for b in c.file_sel.values():
        total |= b
    c.sel = total
    c.note += f" widened({why}):{c.sel.bit_count()}"


# ------------------------------------------------------------------ class level


def _attr_readers(path: str, qual: list[str], name: str, cls: ast.ClassDef, tree: ast.Module) -> int | None:
    """selector._attr_rule, also for classes built by a metaclass that is defined in the
    same file (its code is recorded like any other)."""
    if cls.decorator_list:
        return None
    for k in cls.keywords:
        if k.arg != "metaclass" or not isinstance(k.value, ast.Name):
            return None
        if not any(isinstance(st, ast.ClassDef) and st.name == k.value.id for st in tree.body):
            return None
    saved = cls.keywords
    cls.keywords = []
    try:
        return S._attr_rule(path, qual, name, cls)
    finally:
        cls.keywords = saved


def narrow_class_level(c, cov) -> None:
    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or c.tag.get(path) in S._FUNCTION_TAGS:
            continue
        edit, old_tree = edit_of(c, path)
        if edit is None:
            continue
        qual, a, b, cls = edit
        if not qual:
            continue
        if _is_special(path, qual):
            # Members of an enum (or fields of a dataclass / named tuple) are also reached by
            # iterating the class or by value, which no "who names it" rule sees.
            widen_to_importers(c, path, cov, "enum-like class")
            continue
        if b is None and USE_IMPORT_CHECK and not c.file_sel.get(path):
            widen_for_import_failures(c, path, cov)
        if not c.file_sel.get(path):
            continue
        name, old_value = S._assign(a)
        if name is None or sum(1 for st in cls.body if S._assign(st)[0] == name) != 1:
            continue
        if b is None:
            # The assignment was deleted: reads that resolved to it now resolve elsewhere, or
            # raise. A read while some module is imported breaks every importer of that module.
            if USE_IMPORT_CHECK and widen_for_import_failures(c, path, cov):
                continue
            affected = _attr_readers(path, qual, name, cls, old_tree) if S.USE_ATTR else None
            if affected is not None:
                S.narrow_file(c, path, affected, "attr-del")
            continue
        name2, new_value = S._assign(b)
        if name != name2:
            continue
        if not import_safe(old_value, new_value):
            # The new value runs code while the module is imported.
            if not USE_IMPORT_CHECK:
                continue
            if widen_for_import_failures(c, path, cov):
                continue
        affected = None
        how = ""
        if S.USE_DICT and isinstance(old_value, ast.Dict) and isinstance(new_value, ast.Dict):
            affected = S._dict_rule(f"{path}|{'.'.join(qual)}.{name}", old_value, new_value)
            how = "dict"
        if affected is None and S.USE_ATTR:
            affected = _attr_readers(path, qual, name, cls, old_tree)
            how = "attr"
        if affected is not None:
            S.narrow_file(c, path, affected, how)


# ----------------------------------------------------------------- module level

_SAFE_CALLS = {"frozenset", "set", "dict", "tuple", "list", "TypeVar", "getLogger", "compile", "NewType"}


def _no_side_effects(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if fname not in _SAFE_CALLS:
                return False
        elif isinstance(n, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
            return False
    return True


def _removed_names(a, b, idents: set[str]) -> list[str] | None:
    """Names that stop existing at module level, or None when the edit is something else."""
    if b is None:
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None if a.decorator_list else [a.name]
        if isinstance(a, ast.Assign):
            if all(isinstance(t, ast.Name) for t in a.targets) and _no_side_effects(a.value):
                return [t.id for t in a.targets]
            return None
        if isinstance(a, ast.AnnAssign):
            if isinstance(a.target, ast.Name) and a.value is not None and _no_side_effects(a.value):
                return [a.target.id]
            return None
        if isinstance(a, ast.Import):
            if any(x.name.split(".")[0] == "sqlglot" for x in a.names):
                return None  # importing a sqlglot module runs it: more than a name is lost
            return [(x.asname or x.name).split(".")[0] for x in a.names]
        if isinstance(a, ast.ImportFrom):
            if a.level or (a.module or "").split(".")[0] in ("sqlglot", "tests") or any(x.name == "*" for x in a.names):
                return None
            return [x.asname or x.name for x in a.names]
        return None
    if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) and type(a) is type(b) and a.name != b.name:
        # Renamed at its definition: the old name is gone; the new one must be new to the code base.
        if a.decorator_list or b.name in idents:
            return None
        saved = b.name
        b.name = a.name
        same = _DUMP(a) == _DUMP(b)
        b.name = saved
        return [a.name] if same else None
    return None


def narrow_module_level(c, cov=None) -> None:
    m = S._map(MODREFS)
    if m is None:
        return
    for path, new in c.overlay.items():
        if not path.startswith("sqlglot/") or not c.file_sel.get(path) or c.tag.get(path) in S._FUNCTION_TAGS:
            continue
        names_of = m.raw["names"].get(path)
        if names_of is None:
            continue
        edit, _ = edit_of(c, path)
        if edit is None or edit[0]:
            continue
        _, a, b, _ = edit
        if not hasattr(m, "idents"):
            m.idents = set(m.raw["idents"])
        removed = _removed_names(a, b, m.idents)
        if not removed:
            continue
        affected = 0
        for name in removed:
            if name not in names_of:
                affected = None
                break
            affected |= m.bits(names_of[name])
        failed = package_import_failures(c) if USE_IMPORT_CHECK and cov is not None else set()
        if affected is None:
            if failed:
                widen_for_import_failures(c, path, cov)
            continue
        # Plus tests that fetched the name from the module object (dynamic getattr, from-
        # imports, unpickling: map/modattr.json.gz), so a reference this map cannot see stays.
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        if S.base._modattr() is None:
            continue
        for name in removed:
            affected |= S.base._modattr_bits(module, name)
            for p in S.base._star_importers(module):
                affected |= S.base._modattr_bits(p, name)
        # Tests that execute a reference to the name, plus every importer of a module that
        # no longer imports (normally the same tests: the reference runs at import).
        for f in failed:
            affected |= cov.importers(f) & ((1 << cov.n) - 1)
        S.narrow_file(c, path, affected, "modref")
