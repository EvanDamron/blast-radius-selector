#!/usr/bin/env python3
"""Per TPC-DS entry, for the code-generation proof: the lines run before execution
proper (parse, the executor's optimize, planning, PythonExecutor.generate, DuckDB
transpile) and the lines run at execution time (pickling, schema and tables, running
the generated code), and the codegen digest. Full run traced in a fresh process.

usage: trace_stages.py SRC_DIR INDEX OUT.json
"""
import json
import sys
import time

src, index, out = __import__("os").path.abspath(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
prefix = src.rstrip("/") + "/"
mon = sys.monitoring
TOOL = 3
lines = {"early": {}, "late": {}, "body": {}}
current = ["late"]


def _line(code, ln):
    f = code.co_filename
    if f.startswith(prefix):
        key = current[0] if code.co_flags & 1 else "body"
        lines[key].setdefault(f[len(prefix):], set()).add(ln)
    return mon.DISABLE


def _start(code, off):
    f = code.co_filename
    if not f.startswith(prefix):
        return mon.DISABLE
    first = None
    for _, _, ln in code.co_lines():
        if ln is not None:
            first = ln
            break
    if first is not None:
        key = current[0] if code.co_flags & 1 else "body"
        lines[key].setdefault(f[len(prefix):], set()).add(first)
    return mon.DISABLE


def phase(name):
    current[0] = name
    mon.restart_events()


mon.use_tool_id(TOOL, "brstages")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.register_callback(TOOL, mon.events.PY_START, _start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)

sys.path.insert(0, src)
sys.path.insert(1, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import tpcds_harness  # noqa: E402
from tests.helpers import load_sql_fixture_pairs  # noqa: E402

pair = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))[index]

# Stages whose output the codegen digest covers count as early in every run: parsing,
# the executor's optimize, planning, PythonExecutor.generate, the DuckDB transpile, and
# Expression.sql (execute() renders the optimized tree for a debug log; the digest holds it).
import sqlglot  # noqa: E402
import sqlglot.executor as ex  # noqa: E402
import sqlglot.executor.python as py  # noqa: E402
import sqlglot.planner as planner  # noqa: E402
from sqlglot.expressions.core import Expr, Expression  # noqa: E402


def staged(fn):
    def wrapper(*args, **kwargs):
        before = current[0]
        phase("early")
        try:
            return fn(*args, **kwargs)
        finally:
            phase(before)
    return wrapper


for owner, name in ((sqlglot, "parse_one"), (sqlglot, "transpile"), (ex, "optimize"), (planner.Plan, "__init__"),
                    (py.PythonExecutor, "generate"), (Expr, "sql"), (Expression, "sql")):
    setattr(owner, name, staged(getattr(owner, name)))
tables = tpcds_harness.load_tables()
digest = tpcds_harness.codegen_digest(pair, tables, phase)
phase("late")
full = tpcds_harness.run_entry(pair, tables)  # the execution-time stages, for coverage
mon.set_events(TOOL, 0)
t = time.process_time()
tpcds_harness.codegen_digest(pair, tables)
early_ms = round((time.process_time() - t) * 1000)
json.dump({"index": index, "early": digest, "full": full, "early_ms": early_ms,
           "lines": {k: {p: sorted(v) for p, v in d.items()} for k, d in lines.items()}}, open(out, "w"))
