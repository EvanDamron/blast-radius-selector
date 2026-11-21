"""Per-process line tracer for building the coverage map.

Active only when BRTRACE_DIR is set. Records every (file, line) executed under
/src (body-level code keyed as "B"+path) and every file under /src opened, then writes BRTRACE_DIR/<pid>.json at exit.
Inherited by spawned children and subprocesses through the environment.
"""
import os
import sys

_dir = os.environ.get("BRTRACE_DIR")
if _dir:
    import atexit
    import json

    _PREFIX = os.environ.get("BRTRACE_PREFIX", "/src/")
    _PL = len(_PREFIX)
    _lines = {}
    _opened = set()
    _mon = sys.monitoring
    _TOOL = 4
    _DISABLE = _mon.DISABLE

    _classes = set()
    _getframe = sys._getframe

    def _start(code, offset):
        # Expr.__init__ is starting: note the concrete expression class.
        try:
            cls = type(_getframe(1).f_locals["self"])
            _classes.add(cls.__module__ + ":" + cls.__qualname__)
        except Exception:
            pass

    def _line(code, line):
        f = code.co_filename
        if code.co_qualname == "Expr.__init__" and f.endswith("sqlglot/expressions/core.py"):
            _mon.set_local_events(_TOOL, code, _mon.events.PY_START)
        if f.startswith(_PREFIX):
            # Function code (CO_OPTIMIZED) and module/class-body code are kept apart:
            # a lambda in a class-level dict shares its first line with the dict.
            if not code.co_flags & 1:
                f = "B" + f
            s = _lines.get(f)
            if s is None:
                s = _lines[f] = set()
            s.add(line)
        return _DISABLE

    def _audit(event, args):
        if event == "open" and args and isinstance(args[0], str):
            p = args[0]
            if not p.startswith("/"):
                p = os.path.join(os.getcwd(), p)
            p = os.path.normpath(p)
            if p.startswith(_PREFIX) and not p.endswith((".py", ".pyc")):
                _opened.add(p)

    def _dump():
        try:
            _mon.set_events(_TOOL, 0)
        except Exception:
            pass
        def rel(k):
            return "B" + k[1 + _PL:] if k.startswith("B") else k[_PL:]

        with open(os.path.join(_dir, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"lines": {rel(k): sorted(v) for k, v in _lines.items()},
                       "opened": sorted(p[_PL:] for p in _opened),
                       "classes": sorted(_classes)}, fh)

    _mon.use_tool_id(_TOOL, "brtrace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _start)
    _mon.set_events(_TOOL, _mon.events.LINE)
    sys.addaudithook(_audit)
    atexit.register(_dump)
