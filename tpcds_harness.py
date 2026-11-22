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
