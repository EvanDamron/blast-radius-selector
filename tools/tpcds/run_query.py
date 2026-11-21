#!/usr/bin/env python3
"""Do what TestExecutor.test_execute_tpcds does for some TPC-DS entries.

usage: run_query.py SRC_DIR OUT.json INDEX [INDEX ...]

Per entry of tpc-ds.sql, as the test does: parse it in the parent, send it to a
pool worker (a pickle round trip), and if its meta says execute, run it there on
the tables find_tables names and transpile the SQL to DuckDB for the expected
side. Writes {index: [digest, cpu ms]}. Run one entry per process under
tools/trace to record that entry's coverage.
"""
import json
import sys
import time

src, out, indices = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]
sys.path.insert(0, src)
sys.path.insert(1, str(__import__("pathlib").Path(__file__).resolve().parents[2]))  # tpcds_harness

from tpcds_harness import load_tables, run_entry  # noqa: E402

from tests.helpers import load_sql_fixture_pairs  # noqa: E402

pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
tables = load_tables() if any(pairs[i][0].get("execute") for i in indices) else None
result = {}
for i in indices:
    t = time.process_time()
    digest = run_entry(pairs[i], tables)
    result[i] = [digest, round((time.process_time() - t) * 1000)]
json.dump(result, open(out, "w"))
