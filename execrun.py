"""Run sqlglot tests against a patched source tree inside a forked child.

The patch is applied in memory: an import hook serves the patched text of each
changed module, everything else is imported from the pinned checkout. Tests run
through unittest in the order pytest would run them. The child reports each
test's outcome on a pipe as it finishes, so a hang or crash loses only the tests
that had not finished.
"""

from __future__ import annotations

import concurrent.futures as cf
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import pickle
import sys
import unittest


DEBUG = os.environ.get("BR_DEBUG") == "1"


class SyncPool:
    """In-process stand-in for ProcessPoolExecutor: same calls, pickled both ways.

    Spawned pool workers would import the pinned checkout from disk, not the
    patched modules, so the pool's work is done here instead.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def shutdown(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001 - handed to the caller like a real pool does
            f.set_exception(e)
        return f


class _OverlayLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, text):
        super().__init__(fullname, path)
        self._text = text

    def get_data(self, path):
        if path == self.path:
            return self._text.encode("utf-8")
        return super().get_data(path)

    def get_code(self, fullname):
        return self.source_to_code(self._text.encode("utf-8"), self.path)


class _OverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, src: str, overlay: dict[str, str]):
        self.by_module = {}
        for rel, text in overlay.items():
            if not rel.endswith(".py"):
                continue
            parts = rel[:-3].split("/")
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            self.by_module[".".join(parts)] = (os.path.join(src, rel), text, is_pkg)

    def find_spec(self, fullname, path=None, target=None):
        hit = self.by_module.get(fullname)
        if hit is None:
            return None
        file, text, is_pkg = hit
        loader = _OverlayLoader(fullname, file, text)
        return importlib.util.spec_from_file_location(
            fullname, file, loader=loader, submodule_search_locations=[os.path.dirname(file)] if is_pkg else None
        )


class _Result(unittest.TestResult):
    def __init__(self, emit, numbers_by_id, numbers_by_class):
        super().__init__()
        self.emit = emit
        self.by_id = numbers_by_id
        self.by_class = numbers_by_class
        self.bad = set()

    def _mark(self, test):
        tid = getattr(test, "id", None)
        n = self.by_id.get(tid()) if tid else None
        if n is not None:
            self.bad.add(n)
            return
        # setUpClass / tearDownClass / module-level errors arrive as an _ErrorHolder
        desc = str(getattr(test, "description", "")) or str(test)
        for cls_name, numbers in self.by_class.items():
            if cls_name in desc:
                self.bad.update(numbers)

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test)

    def stopTest(self, test):
        super().stopTest(test)
        n = self.by_id.get(test.id())
        if n is not None:
            self.emit(n, n in self.bad)


def run_tests(tests: list[tuple[int, str, str, str]], write, stop_on_fail: bool = False) -> None:
    """Run tests (number, file, class, method) in the given order; write("<n> <0|1>") per
    finished test and write("done") when every test ran. With stop_on_fail the run ends
    at the first failure without "done": the caller keeps the tests that did not run."""

    def emit(n, bad):
        write(f"{n} {int(bad)}")

    import time as _t
    by_file: dict[str, list] = {}
    for t in tests:
        by_file.setdefault(t[1], []).append(t)
    for file, group in by_file.items():
        try:
            _t0 = _t.process_time()
            mod = importlib.import_module(file[:-3].replace("/", "."))
            if DEBUG:
                write(f"T imp={_t.process_time() - _t0:.2f}")
        except BaseException:  # noqa: BLE001 - a file that cannot be imported fails all its tests
            for t in group:
                emit(t[0], True)
            if stop_on_fail:
                return
            continue
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = SyncPool
        by_class: dict[str, list] = {}
        for t in group:
            by_class.setdefault(t[2], []).append(t)
        for cls_name, members in by_class.items():
            try:
                cls = getattr(mod, cls_name)
                cases = [cls(t[3]) for t in members]
            except BaseException:  # noqa: BLE001
                for t in members:
                    emit(t[0], True)
                if stop_on_fail:
                    return
                continue
            ids = {case.id(): t[0] for case, t in zip(cases, members)}
            result = _Result(emit, ids, {cls_name: [t[0] for t in members]})
            result.failfast = stop_on_fail
            _t0 = _t.process_time()
            try:
                unittest.TestSuite(cases).run(result)
            except BaseException:  # noqa: BLE001
                result.bad.update(t[0] for t in members)
            if DEBUG:
                write(f"T run={_t.process_time() - _t0:.2f}")
            # class-level errors found after stopTest (tearDownClass) and tests never started
            for t in members:
                if t[0] in result.bad:
                    emit(t[0], True)
            if stop_on_fail and result.bad:
                return
    write("done")


def child_main(src: str, overlay: dict[str, str], tests: list[tuple[int, str, str, str]], wfd: int,
               stop_on_fail: bool = False) -> None:
    """Runs in a child forked from a process that has not imported sqlglot."""
    import gc

    gc.enable()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    out = os.fdopen(wfd, "w", buffering=1)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    os.chdir(src)
    sys.path.insert(0, src)
    if overlay:
        sys.meta_path.insert(0, _OverlayFinder(src, overlay))
    sys.dont_write_bytecode = True
    run_tests(tests, lambda line: out.write(line + "\n"), stop_on_fail)
    out.flush()
    os._exit(0)
