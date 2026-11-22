#!/usr/bin/env python3
"""usage: run_dialect.py SRC_DIR INDEX OUT.json — one test_generator_memory subtest, timed."""
import json
import sys
import time

src, index, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sys.path.insert(0, src)
sys.path.insert(1, str(__import__("pathlib").Path(__file__).resolve().parents[2]))  # genmem_harness
import genmem_harness  # noqa: E402

name = genmem_harness.dialect_names()[index]
t = time.process_time()
ok = genmem_harness.run_dialect(name)
json.dump({"name": name, "ok": ok, "ms": round((time.process_time() - t) * 1000)}, open(out, "w"))
