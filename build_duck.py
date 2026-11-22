"""Build step: DuckDB's side of TestExecutor.test_execute_tpcds at the pinned commit.

usage: python build_duck.py SRC OUT.pkl

For each executed TPC-DS entry: the DuckDB transpile of its SQL and the frame DuckDB
returns for it, computed exactly as the test's cached_execute does (setUpClass's
views over the fixture CSVs). The selector's run of the test reuses a frame only when
the patched transpile is the same string, so DuckDB would run the same query on the
same data.
"""
import os
import pickle
import sys

src, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.environ.setdefault("SKIP_INTEGRATION", "0")
os.chdir(src)
sys.path.insert(0, src)

from sqlglot import transpile  # noqa: E402

import tests.test_executor as te  # noqa: E402

te.TestExecutor.setUpClass()
cache = {}
for i, (meta, sql, _) in enumerate(te.TestExecutor.tpcds_sqls):
    if not meta.get("execute"):
        continue
    duck_sql = transpile(sql, write="duckdb")[0]
    cache[i] = (duck_sql, te.TestExecutor.tpcds_conn.execute(duck_sql).fetchdf())
with open(out, "wb") as fh:
    pickle.dump(cache, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(len(cache), "frames")
