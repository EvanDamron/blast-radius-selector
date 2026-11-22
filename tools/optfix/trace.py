#!/usr/bin/env python3
"""Per-entry coverage of the fixture-driven tests in tests/test_optimizer.py.

usage: trace.py SRC OUT.json.gz [JOBS]

The tests whose body loops over load_sql_fixture_pairs (directly or through
check_file) are run for real, each in a forked child of a warm process that has
imported sqlglot and the test module. In each child the module's
load_sql_fixture_pairs yields one entry only (or none: the test's skeleton), and
the child records every line it runs under SRC. The warm process records the lines
run while importing. Pool work runs in-process (execrun.SyncPool), so an entry's
work is traced in the child that yields it.

Output:
{"import": {path: [line, ...]},                   # run while importing (any line)
 "tests": {name: {"files": [file, ...],             # loader arguments, in order
                  "entries": [[file, index, cpu_ms], ...],
                  "skeleton": {path: [line, ...]},  # the run with no entries
                  "body": {path: [line, ...]},      # module/class-level code any run ran
                  "lines": {path: {line: [entry#, ...]}},
                  "ok": true}}}
"""
import gzip
import json
import os
import select
import sys
import time
import unittest

SRC = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]
JOBS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX = SRC + "/"
PL = len(PREFIX)
mon = sys.monitoring


class Tracer:
    """Lines run under SRC: function code by path, module/class-level code as "B"+path."""

    def __init__(self, tool):
        self.tool = tool
        self.lines = {}

    def _add(self, code, line):
        f = code.co_filename
        if not f.startswith(PREFIX):
            return
        key = f[PL:] if code.co_flags & 1 else "B" + f[PL:]
        s = self.lines.get(key)
        if s is None:
            s = self.lines[key] = set()
        s.add(line)

    def _start(self, code, offset):
        if not code.co_filename.startswith(PREFIX):
            return mon.DISABLE
        # LINE events skip the line a function starts on: record it (and the line
        # right after RESUME, e.g. a decorated one-liner) here.
        first = None
        for _, _, ln in code.co_lines():
            if ln is None:
                continue
            if first is None:
                first = ln
                self._add(code, ln)
            elif ln != first:
                self._add(code, ln)
                break
        return mon.DISABLE

    def _line(self, code, line):
        self._add(code, line)
        return mon.DISABLE

    def start(self):
        mon.use_tool_id(self.tool, "optfix")
        mon.register_callback(self.tool, mon.events.LINE, self._line)
        mon.register_callback(self.tool, mon.events.PY_START, self._start)
        mon.set_events(self.tool, mon.events.LINE | mon.events.PY_START)

    def stop(self):
        mon.set_events(self.tool, 0)
        mon.register_callback(self.tool, mon.events.LINE, None)
        mon.register_callback(self.tool, mon.events.PY_START, None)
        mon.free_tool_id(self.tool)


os.chdir(SRC)
sys.path.insert(0, SRC)
sys.path.insert(1, ROOT)
os.environ.setdefault("SKIP_INTEGRATION", "0")
sys.dont_write_bytecode = True

imp = Tracer(5)
imp.start()
import sqlglot  # noqa: E402,F401
import tests.test_optimizer as T  # noqa: E402
from tests.helpers import load_sql_fixture_pairs as original  # noqa: E402
imp.stop()

from execrun import SyncPool  # noqa: E402

T.ProcessPoolExecutor = SyncPool
KEEP = None      # None: every entry; else a set of (file, index)
ASKED = []       # loader arguments seen in this process


def filtered(filename):
    ASKED.append(filename)
    for i, item in enumerate(original(filename)):
        if KEEP is None or (filename, i) in KEEP:
            yield item


T.load_sql_fixture_pairs = filtered


def loop_tests():
    import ast

    src = open(T.__file__).read()
    names = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "TestOptimizer":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name.startswith("test_"):
                    seg = ast.get_source_segment(src, f)
                    if "check_file(" in seg or "load_sql_fixture_pairs(" in seg:
                        names.append(f.name)
    return names


def run_child(name, keep, trace):
    """Fork; the child runs test `name` with KEEP=keep. Returns (pid, read fd)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            global KEEP
            KEEP = keep
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            tr = Tracer(4) if trace else None
            if tr:
                tr.start()
            res = unittest.TestResult()
            unittest.TestSuite([T.TestOptimizer(name)]).run(res)
            if tr:
                tr.stop()
            out = {"ok": res.wasSuccessful(), "asked": ASKED,
                   "lines": {k: sorted(v) for k, v in (tr.lines if tr else {}).items()}}
            data = json.dumps(out).encode()
            while data:
                n = os.write(w, data[:65536])
                data = data[n:]
        finally:
            os._exit(0)
    os.close(w)
    return pid, r


def run_many(jobs):
    """jobs: list of (key, name, keep, trace). Returns {key: (result dict, cpu seconds)}."""
    results = {}
    queue = list(jobs)
    active = {}
    while queue or active:
        while queue and len(active) < JOBS:
            key, name, keep, trace = queue.pop(0)
            pid, fd = run_child(name, keep, trace)
            active[fd] = [key, pid, b""]
        ready, _, _ = select.select(list(active), [], [])
        for fd in ready:
            chunk = os.read(fd, 1 << 20)
            if chunk:
                active[fd][2] += chunk
                continue
            key, pid, data = active.pop(fd)
            os.close(fd)
            _, _, ru = os.wait4(pid, 0)
            results[key] = (json.loads(data) if data else {"ok": False, "lines": {}, "asked": []},
                            ru.ru_utime + ru.ru_stime)
    return results


def main():
    names = loop_tests()
    t0 = time.time()
    skel = run_many([(n, n, set(), True) for n in names])
    out = {"import": {k: sorted(v) for k, v in imp.lines.items() if not k.startswith("B")},
           "import_body": {k[1:]: sorted(v) for k, v in imp.lines.items() if k.startswith("B")},
           "tests": {}}
    jobs = []
    for n in names:
        files = list(dict.fromkeys(skel[n][0]["asked"]))
        entries = [(f, i) for f in files for i in range(sum(1 for _ in original(f)))]
        out["tests"][n] = {"files": files, "entries": [[f, i, 0] for f, i in entries], "ok": skel[n][0]["ok"]}
        for k, e in enumerate(entries):
            jobs.append(((n, k, "t"), n, {e}, True))
            jobs.append(((n, k, "c"), n, {e}, False))
    print(f"{len(names)} tests, {len(jobs) // 2} entries", flush=True)
    res = run_many(jobs)
    for n in names:
        t = out["tests"][n]
        sk = skel[n][0]["lines"]
        t["skeleton"] = {k: v for k, v in sk.items() if not k.startswith("B")}
        body = {k[1:]: set(v) for k, v in sk.items() if k.startswith("B")}
        lines = {}
        base_cpu = skel[n][1]
        for k, e in enumerate(t["entries"]):
            r, _ = res[(n, k, "t")]
            rc, cpu = res[(n, k, "c")]
            t["ok"] = t["ok"] and r["ok"] and rc["ok"]
            e[2] = max(0, round((cpu - base_cpu) * 1000))
            for path, ls in r["lines"].items():
                if path.startswith("B"):
                    body.setdefault(path[1:], set()).update(ls)
                    continue
                skl = set(t["skeleton"].get(path, ()))
                d = lines.setdefault(path, {})
                for ln in ls:
                    if ln not in skl:
                        d.setdefault(ln, []).append(k)
        t["body"] = {k: sorted(v) for k, v in body.items()}
        t["lines"] = {p: {str(l): e for l, e in d.items()} for p, d in lines.items() if d}
        print(n, len(t["entries"]), "entries", "ok" if t["ok"] else "NOT OK",
              f"skeleton cpu {base_cpu:.2f}s", flush=True)
    with gzip.open(OUT, "wt", compresslevel=9) as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
