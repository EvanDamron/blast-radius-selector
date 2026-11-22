"""The per-entry work of TestExecutor.test_execute_tpcds, outside unittest.

Shared by the map builder (tools/tpcds/) and the selector's proof step, so both
compute exactly the same digest. Imports sqlglot lazily: the caller decides which
sqlglot (pinned or patched) is on the import path.
"""
import ast
import csv
import gzip
import hashlib
import pickle
import re


class LazyTables(dict):
    """The TPC-DS tables as TestExecutor.setUpClass builds them, each loaded on first
    use. A name outside the schema raises KeyError, as the test's own dict would."""

    def __missing__(self, table):
        from tests.helpers import TPCDS_SCHEMA

        if table not in TPCDS_SCHEMA:
            raise KeyError(table)
        value = self[table] = _load_table(table, TPCDS_SCHEMA[table])
        return value


def load_tables():
    return LazyTables()


def _load_table(table, columns):
    from sqlglot.executor.table import Table
    from tests.helpers import FIXTURES_DIR

    with gzip.open(f"{FIXTURES_DIR}/optimizer/tpc-ds/{table}.csv.gz", "rt", newline="") as fh:
        reader = csv.reader(fh, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def run_entry(pair, tables):
    """Digest of everything the test derives from one entry with sqlglot."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA

    meta, sql, _ = pair
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return "parsed"
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    result = execute(expression, schema=TPCDS_SCHEMA, tables=used)
    result = pickle.loads(pickle.dumps(result))
    expected_sql = transpile(sql, write="duckdb")[0]
    return hashlib.sha256(repr((result.columns, result.rows, expected_sql)).encode()).hexdigest()


class _Stop(BaseException):
    pass


def _canon(node):
    """A deterministic structure of an expression tree: class, args, type, comments."""
    from sqlglot.expressions.core import Expr

    if isinstance(node, Expr):
        return (type(node).__qualname__,
                tuple((k, _canon(v)) for k, v in sorted(node.args.items())),
                _canon(getattr(node, "_type", None)),
                tuple(node.comments or ()))
    if isinstance(node, (list, tuple)):
        return tuple(_canon(x) for x in node)
    return repr(node)


def early_digest(pair, tables, on_phase=None):
    """Digest of what the test derives from one entry before execution starts: the
    expression the executor's optimize() returns inside execute(), and the DuckDB
    transpile. Execution itself is skipped. on_phase("early"/"late") marks stages."""
    import sqlglot.executor as ex
    from sqlglot import find_tables, parse_one, transpile
    from tests.helpers import TPCDS_SCHEMA

    phase = on_phase or (lambda name: None)
    meta, sql, _ = pair
    phase("early")
    parsed = parse_one(sql)
    phase("late")
    expression = pickle.loads(pickle.dumps(parsed))
    if not meta.get("execute"):
        return "parsed"
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    captured = []
    original = ex.optimize

    def optimize(*args, **kwargs):
        phase("early")
        out = original(*args, **kwargs)
        phase("late")
        captured.append(out)
        raise _Stop()

    ex.optimize = optimize
    try:
        ex.execute(expression, schema=TPCDS_SCHEMA, tables=used)
    except _Stop:
        pass
    finally:
        ex.optimize = original
    phase("early")
    expected_sql = transpile(sql, write="duckdb")[0]
    phase("late")
    return hashlib.sha256(repr((_canon(captured[0]) if captured else None, expected_sql)).encode()).hexdigest()


def _canon_value(v):
    from sqlglot.expressions.core import Expr

    if isinstance(v, Expr):
        return _canon(v)
    if isinstance(v, dict):
        return tuple(sorted((repr(k), _canon_value(x)) for k, x in v.items()))
    if isinstance(v, (list, tuple)):
        return tuple(_canon_value(x) for x in v)
    if isinstance(v, (set, frozenset)):
        return tuple(sorted(repr(_canon_value(x)) for x in v))
    if hasattr(v, "dependencies") and hasattr(v, "name"):
        return ("step", v.name)
    return repr(v)


def _plan_canon(plan):
    steps = sorted(plan.dag, key=lambda s: s.name)
    out = []
    for s in steps:
        attrs = tuple((k, _canon_value(x)) for k, x in sorted(vars(s).items()) if k not in ("dependencies", "dependents"))
        out.append((type(s).__qualname__, s.name, tuple(sorted(d.name for d in s.dependencies)), attrs))
    return tuple(out)


def _exprs_in(v):
    from sqlglot.expressions.core import Expr

    if isinstance(v, Expr):
        yield v
    elif isinstance(v, dict):
        for k in sorted(v, key=repr):
            yield from _exprs_in(v[k])
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _exprs_in(x)


def codegen_digest(pair, tables, on_phase=None):
    """Digest of everything the test's execution depends on except the executor's
    runtime: the optimized tree, the plan (and every subquery plan), the Python source
    PythonGenerator emits for every expression of every step (a superset of what the
    executor compiles), and the DuckDB transpile. Nothing is executed."""
    import sqlglot.executor as ex
    import sqlglot.executor.python as py
    from sqlglot import find_tables, parse_one, transpile
    from tests.helpers import TPCDS_SCHEMA

    phase = on_phase or (lambda name: None)
    meta, sql, _ = pair
    phase("early")
    parsed = parse_one(sql)
    phase("late")
    expression = pickle.loads(pickle.dumps(parsed))
    if not meta.get("execute"):
        return "parsed"
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    captured = {}
    original_optimize, original_execute = ex.optimize, py.PythonExecutor.execute

    def optimize(*args, **kwargs):
        phase("early")
        out = original_optimize(*args, **kwargs)
        phase("late")
        captured["optimized"] = out
        return out

    def execute(self, plan, outer_scope=None):
        captured["executor"], captured["plan"] = self, plan
        raise _Stop()

    ex.optimize, py.PythonExecutor.execute = optimize, execute
    try:
        ex.execute(expression, schema=TPCDS_SCHEMA, tables=used)
    except _Stop:
        pass
    finally:
        ex.optimize, py.PythonExecutor.execute = original_optimize, original_execute
    if "plan" not in captured:
        return "no-plan"
    exe = captured["executor"]
    phase("early")

    def step_canon(step, memo):
        key = id(step)
        if key not in memo:
            sources = []
            for name, value in sorted(vars(step).items()):
                if name in ("dependencies", "dependents"):
                    continue
                for e in _exprs_in(value):
                    try:
                        sources.append((name, exe.generator.generate(exe._replace_subqueries(e))))
                    except Exception as error:  # deterministic either way
                        sources.append((name, "error:" + type(error).__name__))
            attrs = tuple((k, _canon_value(x)) for k, x in sorted(vars(step).items())
                          if k not in ("dependencies", "dependents"))
            deps = sorted((step_canon(d, memo) for d in step.dependencies), key=repr)
            memo[key] = (type(step).__qualname__, step.name, attrs, tuple(sources), tuple(deps))
        return memo[key]

    memo = {}
    main = step_canon(captured["plan"].root, memo)
    done, subs = 0, []
    while done < len(exe._subquery_plans):  # plans registered while generating code
        plans = list(exe._subquery_plans.values())
        for plan, _ in plans[done:]:
            subs.append(repr(step_canon(plan.root, memo)))
        done = len(plans)
    expected_sql = transpile(sql, write="duckdb")[0]
    phase("late")
    logged = captured["optimized"].sql(pretty=True)  # execute() builds this for a debug log
    text = repr((_canon(captured["optimized"]), logged, main, sorted(subs), expected_sql))
    # Subquery plans are numbered in registration order; the executor's own numbering is
    # a fixed function of the plan, so the numbers carry nothing.
    return hashlib.sha256(re.sub(r"_sq_\d+", "_sq_", text).encode()).hexdigest()
