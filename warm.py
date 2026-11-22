"""A warm process that keeps the pinned sqlglot and its test modules imported and
forks one worker per commit.

A worker gets the patch in one of two ways, both exact:
- a changed module that is not imported yet is served, patched, by the import hook;
- a changed module that is already imported must differ from the pinned one only
  inside function bodies, and none of those functions may have run while the warm
  process was importing. The worker then points every live function object at the
  new code. Anything else is refused and the caller runs the commit in a fresh
  interpreter instead.
"""

from __future__ import annotations

import gc
import importlib
import importlib.util
import marshal
import os
import pickle
import select
import signal
import struct
import sys
import types
import unittest

import execrun

CO_OPTIMIZED = 0x1
_CODE = types.CodeType


def _const_eq(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_const_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, frozenset):
        return sorted(map(repr, a)) == sorted(map(repr, b))
    if isinstance(a, (float, complex)):
        return repr(a) == repr(b)
    return a == b


def _own_eq(a: _CODE, b: _CODE) -> bool:
    """Same code apart from line numbers and nested code objects."""
    if (
        a.co_code != b.co_code
        or a.co_names != b.co_names
        or a.co_varnames != b.co_varnames
        or a.co_freevars != b.co_freevars
        or a.co_cellvars != b.co_cellvars
        or a.co_argcount != b.co_argcount
        or a.co_posonlyargcount != b.co_posonlyargcount
        or a.co_kwonlyargcount != b.co_kwonlyargcount
        or a.co_flags != b.co_flags
        or a.co_name != b.co_name
        or a.co_qualname != b.co_qualname
        or a.co_exceptiontable != b.co_exceptiontable
        or len(a.co_consts) != len(b.co_consts)
    ):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        if isinstance(x, _CODE):
            if not isinstance(y, _CODE):
                return False
        elif not _const_eq(x, y):
            return False
    return True


def _deep_eq(a: _CODE, b: _CODE) -> bool:
    if not _own_eq(a, b):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        if isinstance(x, _CODE) and not _deep_eq(x, y):
            return False
    return True


def _all_nested(co: _CODE, out: list) -> None:
    for x in co.co_consts:
        if isinstance(x, _CODE):
            out.append(x)
            _all_nested(x, out)


def _collect(old: _CODE, new: _CODE, pairs: list, orphans: list, top: bool = True) -> None:
    pairs.append((old, new, top))
    oc = [c for c in old.co_consts if isinstance(c, _CODE)]
    nc = [c for c in new.co_consts if isinstance(c, _CODE)]
    if len(oc) == len(nc) and all(a.co_name == b.co_name for a, b in zip(oc, nc)):
        for a, b in zip(oc, nc):
            if not _deep_eq(a, b):
                _collect(a, b, pairs, orphans, False)
    else:
        _all_nested(old, orphans)


def function_changes(old: _CODE, new: _CODE, pairs: list, orphans: list) -> bool:
    """Compare a module (or class body) code object with its patched version.

    False when anything outside function bodies differs. Otherwise pairs holds
    (old code, new code, outermost) for every changed function, and orphans the old
    nested code objects that have no counterpart.
    """
    if not _own_eq(old, new):
        return False
    for x, y in zip(old.co_consts, new.co_consts):
        if not isinstance(x, _CODE):
            continue
        if x.co_flags & CO_OPTIMIZED:
            if not _deep_eq(x, y):
                _collect(x, y, pairs, orphans)
        elif not function_changes(x, y, pairs, orphans):
            return False
    return True


def _key(co: _CODE, path: str | None = None):
    """A code object's identity. A .pyc keeps the path it was compiled under, so codes
    read from one are keyed by the path the module was actually imported from."""
    return (path or co.co_filename, co.co_firstlineno, co.co_qualname)


def _pinned_code(path: str) -> _CODE:
    """The pinned module's code object: from its .pyc when present, else compiled."""
    try:
        pyc = importlib.util.cache_from_source(path)
        with open(pyc, "rb") as fh:
            data = fh.read()
        return marshal.loads(data[16:])
    except Exception:
        with open(path, "rb") as fh:
            return compile(fh.read(), path, "exec", dont_inherit=True)


class Zygote:
    def __init__(self, src: str, job_r: int, out_w: int):
        self.src = src
        self.job_r = job_r
        self.out = os.fdopen(out_w, "w", buffering=1)
        self.started: set = set()
        self._checked: dict = {}
        self.index: dict | None = None
        self.index_modules = -1
        self.workers: dict[int, int] = {}
        self.prefix = os.path.join(src, "sqlglot") + os.sep

    # -- import tracking

    def _on_start(self, code, offset):
        if code.co_filename.startswith(self.prefix):
            self.started.add(_key(code))
        return sys.monitoring.DISABLE

    def tracked_import(self, names: list[str]) -> list[str]:
        mon = sys.monitoring
        failed = []
        mon.set_events(mon.PROFILER_ID, mon.events.PY_START)
        try:
            for name in names:
                try:
                    mod = importlib.import_module(name)
                    if hasattr(mod, "ProcessPoolExecutor"):
                        mod.ProcessPoolExecutor = execrun.SyncPool
                except BaseException:  # noqa: BLE001
                    failed.append(name)
        finally:
            mon.set_events(mon.PROFILER_ID, 0)
        return failed

    def functions(self) -> dict:
        if self.index is None or self.index_modules != len(sys.modules):
            index: dict = {}
            prefix = self.prefix
            FunctionType = types.FunctionType
            for o in gc.get_objects():
                if type(o) is FunctionType:
                    co = o.__code__
                    if co.co_filename.startswith(prefix):
                        index.setdefault((co.co_filename, co.co_firstlineno, co.co_qualname), []).append(o)
            self.index = index
            self.index_modules = len(sys.modules)
        return self.index

    # -- per job

    def plan(self, overlay: dict[str, str]):
        """Returns (swaps, lazy overlay) or None when the commit needs a fresh interpreter."""
        swaps = []
        lazy = {}
        for rel, text in overlay.items():
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            name = ".".join(parts)
            if name not in sys.modules:
                lazy[rel] = text
                continue
            if not rel.startswith("sqlglot/"):
                return None
            path = getattr(sys.modules[name], "__file__", None)
            if not path:
                return None
            checked = self._structure(rel, text, path)
            if checked is None:
                return None
            new, pairs, orphans = checked
            index = self.functions()
            for old, _, _ in pairs:
                if _key(old, path) in self.started:
                    return None
            for old in orphans:
                if _key(old, path) in self.started or _key(old, path) in index:
                    return None
            for old, new_code, top in pairs:
                live = index.get(_key(old, path), ())
                if top and not live:
                    # No function object to repoint: do not guess, use a fresh interpreter.
                    return None
                for fn in live:
                    if fn.__code__.co_freevars != new_code.co_freevars:
                        return None
                    swaps.append((fn, new_code))
        return swaps, lazy

    def _structure(self, rel: str, text: str, path: str):
        """(patched code, changed functions, orphans), or None when the patch changes
        anything outside function bodies. Cached for the job being handled."""
        if rel not in self._checked:
            try:
                new = compile(text, path, "exec", dont_inherit=True)
            except BaseException:  # noqa: BLE001 - e.g. a syntax error
                self._checked[rel] = None
                return None
            pairs: list = []
            orphans: list = []
            ok = function_changes(_pinned_code(path), new, pairs, orphans)
            self._checked[rel] = (new, pairs, orphans) if ok else None
        return self._checked[rel]

    def refused_early(self, overlay: dict[str, str]) -> bool:
        """True when a module that is already imported changes outside function bodies:
        such a job is refused whatever the test imports do, so skip them."""
        for rel, text in overlay.items():
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mod = sys.modules.get(".".join(parts))
            if mod is None:
                continue
            path = getattr(mod, "__file__", None)
            if not path or not rel.startswith("sqlglot/") or self._structure(rel, text, path) is None:
                return True
        return False

    def handle(self, job: dict) -> None:
        jid = job["id"]
        files = []
        for t in job["tests"]:
            name = t[1][:-3].replace("/", ".")
            if name not in files:
                files.append(name)
        # A test module that is itself patched, or imports a patched module at load, must
        # not be imported here first: plan() below sees what is loaded after this step.
        import time as _t
        self._checked = {}
        if self.refused_early(job["overlay"]):
            self.out.write(f"{jid} cold\n")
            return
        t0 = _t.process_time()
        changed = []
        for rel in job["overlay"]:
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            changed.append(".".join(parts))
        if all(name in sys.modules for name in changed):
            # Nothing is left for the import hook, so test modules can be imported here
            # once and shared by later workers.
            self.tracked_import([f for f in files if f not in sys.modules])
        t1 = _t.process_time()
        plan = self.plan(job["overlay"])
        t2 = _t.process_time()
        if plan is None:
            self.out.write(f"{jid} cold\n")
            return
        swaps, lazy = plan
        gc.freeze()  # the warm process's modules: never garbage, not worth a worker's scans
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                for fn, code in swaps:
                    fn.__code__ = code
                if lazy:
                    sys.meta_path.insert(0, execrun._OverlayFinder(self.src, lazy))
                execrun.run_tests(job["tests"], lambda line: self.out.write(f"{jid} {line}\n"), job.get("stop_on_fail", False))
                self.out.flush()
            finally:
                os._exit(0)
        self.workers[pid] = jid
        self.out.write(f"{jid} pid {pid} swaps={len(swaps)} lazy={len(lazy)} imp={t1 - t0:.2f} plan={t2 - t1:.2f}\n")

    def reap(self, block: bool = False) -> None:
        while self.workers:
            try:
                pid, _ = os.waitpid(-1, 0 if block else os.WNOHANG)
            except ChildProcessError:
                self.workers.clear()
                return
            if pid == 0:
                return
            jid = self.workers.pop(pid, None)
            if jid is not None:
                self.out.write(f"{jid} exit\n")

    def serve(self) -> None:
        gc.enable()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        os.chdir(self.src)
        sys.path.insert(0, self.src)
        sys.dont_write_bytecode = True
        mon = sys.monitoring
        mon.use_tool_id(mon.PROFILER_ID, "blast")
        mon.register_callback(mon.PROFILER_ID, mon.events.PY_START, self._on_start)
        self.tracked_import(["sqlglot"])
        buf = b""
        while True:
            ready, _, _ = select.select([self.job_r], [], [], 0.02 if self.workers else None)
            self.reap()
            if not ready:
                continue
            data = os.read(self.job_r, 1 << 20)
            if not data:
                break
            buf += data
            while len(buf) >= 4:
                (n,) = struct.unpack("<I", buf[:4])
                if len(buf) < 4 + n:
                    break
                job = pickle.loads(buf[4 : 4 + n])
                buf = buf[4 + n :]
                try:
                    self.handle(job)
                except BaseException:  # noqa: BLE001
                    self.out.write(f"{job['id']} cold\n")
        self.reap(block=True)
        self.out.flush()
        os._exit(0)
