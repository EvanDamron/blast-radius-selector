"""Per-process recorder of calls that may have used a parameter's default.

Active only when BRTRACE_DIR is set (inherited by spawned children). For every call
of a function listed in BRDEF_TABLE (see build_table.py), a parameter whose value on
entry is the default literal (identical, or equal and of the same type) is recorded:
that call may have relied on the default. A value that differs proves the caller
passed the argument.

It also records, for every validation of an expression (Expression.error_messages,
the only reader of arg_types values through required_args), which (class, arg)
pairs were missing or empty: only there can flipping an arg_types value matter.
Writes BRTRACE_DIR/<pid>.json at exit.
"""
import os
import sys

_dir = os.environ.get("BRTRACE_DIR")
if _dir:
    import atexit
    import json

    _PREFIX = os.environ.get("BRTRACE_PREFIX", "/src/")
    _PL = len(_PREFIX)
    with open(os.environ["BRDEF_TABLE"]) as _fh:
        _TABLE = json.load(_fh)
    _mon = sys.monitoring
    _TOOL = 4
    _DISABLE = _mon.DISABLE
    _getframe = sys._getframe
    _pending = {}  # code -> (relpath, qualname, [[param, value], ...] not yet seen as default)
    _seen = set()  # (relpath, qualname, param)
    _missing = set()  # (class key, arg)
    _VALIDATE = ("sqlglot/expressions/core.py", "Expression.error_messages")

    def _validate(loc):
        try:
            node = loc["self"]
            cls = type(node)
            args = node.args
            for k in cls.arg_types:
                v = args.get(k)
                if v is None or (isinstance(v, list) and not v):
                    _missing.add((cls.__module__ + ":" + cls.__qualname__, k))
        except Exception:
            pass

    def _start(code, offset):
        p = _pending.get(code)
        if p is None:
            f = code.co_filename
            if not f.startswith(_PREFIX):
                return _DISABLE
            rel = f[_PL:]
            d = _TABLE.get(rel, {}).get(code.co_qualname)
            validate = (rel, code.co_qualname) == _VALIDATE
            if not d and not validate:
                return _DISABLE
            p = _pending[code] = (rel, code.co_qualname, list(d or ()), validate)
        rel, qual, todo, validate = p
        loc = _getframe(1).f_locals
        if validate:
            _validate(loc)
        keep = []
        for item in todo:
            name, lit = item
            try:
                v = loc[name]
            except KeyError:
                keep.append(item)
                continue
            if v is lit or (type(v) is type(lit) and v == lit):
                _seen.add((rel, qual, name))
            else:
                keep.append(item)
        todo[:] = keep
        if not keep and not validate:
            return _DISABLE
        return None

    def _dump():
        try:
            _mon.set_events(_TOOL, 0)
        except Exception:
            pass
        with open(os.path.join(_dir, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"maybe_default": sorted(_seen), "missing_args": sorted(_missing)}, fh)

    _mon.use_tool_id(_TOOL, "brdefaults")
    _mon.register_callback(_TOOL, _mon.events.PY_START, _start)
    _mon.set_events(_TOOL, _mon.events.PY_START)
    atexit.register(_dump)
