"""One dialect's subtest of TestGenerator.test_generator_memory, outside unittest.

Shared by the map builder (tools/genmem/) and the selector's proof step. Imports sqlglot
lazily: the caller decides which sqlglot (pinned or patched) is on the import path.
"""
import gc
import tracemalloc


def dialect_names():
    from sqlglot.dialects.dialect import Dialects

    return [str(d.value) for d in Dialects]


def run_dialect(name: str) -> bool:
    """True when the subtest's assertion holds (as in tests/test_generator.py)."""
    from sqlglot.dialects.dialect import Dialect, Dialects

    dialect = Dialect.get_or_raise(Dialects(name))
    for _ in range(100):
        dialect.generator()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(1000):
            dialect.generator()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return after - before < 1024
