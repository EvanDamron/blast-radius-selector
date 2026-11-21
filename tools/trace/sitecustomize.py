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

    _inits_seen = set()

    def _first_lines(code, f):
        # LINE events skip the line a function starts on (one-line lambdas and
        # defs would look unexecuted), so record the lines run before the first
        # line change here.
        first = None
        for start, end, ln in code.co_lines():
            if ln is None:
                continue
            if first is None:
                first = ln
                _add(code, f, ln)
            elif ln != first:
                _add(code, f, ln)  # the line after RESUME, e.g. a decorated one-liner
                break

    def _start(code, offset):
        f = code.co_filename
        if not f.startswith(_PREFIX):
            return _DISABLE
        if code.co_name == "__init__":
            # Note the concrete class of self. Kept enabled so that every class
            # inheriting this __init__ is seen.
            try:
                cls = type(_getframe(1).f_locals["self"])
                _classes.add(cls.__module__ + ":" + cls.__qualname__)
            except Exception:
                pass
            if code not in _inits_seen:
                _inits_seen.add(code)
                _first_lines(code, f)
            return None
        _first_lines(code, f)
        return _DISABLE

    def _add(code, f, line):
        if not code.co_flags & 1:
            f = "B" + f
        s = _lines.get(f)
        if s is None:
            s = _lines[f] = set()
        s.add(line)

    def _line(code, line):
        f = code.co_filename
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

    _listed = set()

    def _audit(event, args):
        if event in ("os.listdir", "os.scandir", "glob.glob") and args:
            p = args[0]
            if isinstance(p, bytes):
                p = p.decode(errors="replace")
            if isinstance(p, str):
                if not p.startswith("/"):
                    p = os.path.join(os.getcwd(), p)
                p = os.path.normpath(p)
                if p.startswith(_PREFIX) or p + "/" == _PREFIX:
                    # Only listings asked for by sqlglot or test code, not by the
                    # import system or pytest's own collection.
                    f = _getframe(1)
                    depth = 0
                    while f is not None and depth < 25:
                        fn = f.f_code.co_filename
                        if fn.startswith(_PREFIX):
                            _listed.add(p)
                            break
                        if "importlib" in fn or "_pytest" in fn or "pluggy" in fn:
                            break
                        f = f.f_back
                        depth += 1
            return
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
                       "classes": sorted(_classes),
                       "listed": sorted(p[_PL:] for p in _listed)}, fh)

    _mon.use_tool_id(_TOOL, "brtrace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _start)
    _mon.set_events(_TOOL, _mon.events.LINE | _mon.events.PY_START)
    sys.addaudithook(_audit)
    atexit.register(_dump)
