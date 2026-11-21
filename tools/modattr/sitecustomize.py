"""Which module-level names of sqlglot modules each test fetches from outside them.

Active only when BRTRACE_DIR is set (inherited by spawned children). Every sqlglot
module gets a ModuleType subclass as its class before its code runs, whose
__getattribute__ records (module, name): `from m import x`, `m.x`, getattr and
hasattr on m, unpickling by name, mock.patch. Code inside the module reads its own
globals directly and is not recorded (coverage covers that). Writes
BRTRACE_DIR/<pid>.json at exit.
"""
import os
import sys

_dir = os.environ.get("BRTRACE_DIR")
if _dir:
    import atexit
    import importlib.abc
    import json
    import types

    _seen = set()
    _add = _seen.add
    _getattribute = types.ModuleType.__getattribute__

    class _Tracked(types.ModuleType):
        def __getattribute__(self, name):
            if name[:2] != "__":
                _add((_getattribute(self, "__name__"), name))
            return _getattribute(self, name)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find = getattr(finder, "find_spec", None)
                spec = find(fullname, path, target) if find else None
                if spec is not None:
                    break
            else:
                return None
            loader = spec.loader
            exec_module = getattr(loader, "exec_module", None)
            if exec_module is None:
                return spec

            def wrapped(module, _exec=exec_module):
                try:
                    module.__class__ = _Tracked
                except TypeError:
                    pass
                _exec(module)

            try:
                loader.exec_module = wrapped
            except AttributeError:
                pass
            return spec

    sys.meta_path.insert(0, _Finder())

    def _dump():
        with open(os.path.join(_dir, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"modattr": sorted(_seen)}, fh)

    atexit.register(_dump)
