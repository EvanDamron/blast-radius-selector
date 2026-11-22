# Blast Radius test selector

An entry for the RWX "Blast Radius" track of the Builders Buildathon. For each
patch against sqlglot at `eb11aec`, it picks the tests that can observe the
change and splits them across workers.

## How it decides

The maps in `map/` were built once from the pinned commit:

- `cov.json.gz`: every test was run in its own process under a
  `sys.monitoring` line tracer (`tools/trace/sitecustomize.py`, which follows
  spawned children). For each source line it records which tests executed it,
  with function code kept apart from module/class-body code. It also records
  which files each test opened and which sqlglot classes each test
  instantiated.
- `static.json.gz`: every function definition by name, and the expression
  class tree.
- `cmp.json.gz`, `dict.json.gz`, `attr.json.gz` (built by `tools/cmp/`): each
  test ran in its own interpreter against an instrumented copy of the pinned
  source. For every single-operator comparison, they record which other
  operators would have given a different result on the operands that test
  produced. For every class-level dict literal, they record which keys each test
  looked up or tested for membership, and whether it read the dict wholesale. For
  every class-level name, they record whether a test's reads resolved to that
  class's definition.
- `defaults.json.gz`, `argtypes.json.gz` (built by `tools/defaults/`): each test
  ran under a call recorder. For every function parameter with a literal default,
  they record the tests in which some call received a value identical to the
  default (any other value proves the caller passed the argument). For every
  validation of an expression (`Expression.error_messages`, the only reader of
  `arg_types` values), they record which arguments were missing.
- `fixtures.json.gz` (built by `tools/fixtures/`): each test that opens a SQL
  fixture ran once per part of that file (metadata, input SQL, expected SQL, or
  whole entries, as `tests/helpers.py` yields them), with that part replaced by an
  object that fails on first use. A test that passes never reads that part.

At run time `selector.py` applies each patch to the pinned source and compares
the old and new ASTs, one scope at a time:

- **Comments, formatting, docstrings**: no code changed, so nothing is selected.
- **Inside a function**: the tests that executed the changed statement (for an
  insertion, the statements on both sides of it).
- **Lambdas**: the tests that ran the lambda.
- **Module or class level**: a function header selects that function's callers.
  An edit to a dict entry selects the users of the expression classes and
  helpers the entry names. An attribute of an expression class selects the tests
  that instantiated the class or a subclass. Anything else selects the tests that
  ran a line naming the changed attribute and also imported the module. Lazy
  dialect imports keep this narrow. For a dialect family's classes (dialect,
  tokenizer, parser, generator), reads through an instance count only for tests
  that instantiated a class of that family.
- **Syntax errors**: every test that imports the module.
- **Fixtures and other data files**: the tests that opened the file.
- **pytest config**: everything, if the edit can reach pytest.

A second stage (`selector.py`) only removes candidates from the first stage's
answer (`base_selector.py`), and only on proof that a test runs exactly as
before:

- **Only a comparison operator changed**: keep the tests in which the old and the
  new operator disagreed at least once.
- **Only entries of one class-level dict literal changed**: keep the tests that
  looked up a changed key, tested membership of an added or removed key, or read
  the dict wholesale.
- **Only one class-level assignment changed**: keep the tests whose reads
  resolved to that class (not applied to names that also occur as strings, enums,
  dataclasses or decorated classes).
- **Only literal default values changed**: keep the tests in which a call may
  have used the old default.
- **Only True/False values in one `arg_types` literal changed**: keep the tests
  that validated an instance of that class (or a subclass, or a class that copies
  its `arg_types`) with that argument missing.
- **Only function annotations changed** (parameters, returns, local variables, in a
  module with `from __future__ import annotations`): nothing. They are never
  evaluated and nothing reads a function's `__annotations__`; class- and
  module-level annotations are not covered (dataclasses read them).
- **`if TYPE_CHECKING:` flipped to `if not TYPE_CHECKING:`** (a block of imports, no
  else): nothing, provided the patched package still imports in a child process.
  All 62 such blocks in the pinned tree were flipped and labeled: the 13 that break
  tests all fail that import check; the other 49 break nothing.
- **A SQL fixture changed**: keep the tests that read a part of the file that
  differs (a file the helpers can no longer load keeps every test that opens it).
- Tests that open the changed source file as data are always kept.
- **`test_execute_tpcds` (21.5 s)**: `map/tpcds.json.gz` records which source lines
  each TPC-DS entry reaches when run on its own (`tools/tpcds/`), and a digest of
  what the test derives from it (`tpcds_harness.py`: parse, the pool's pickle
  round trip, execution, the DuckDB transpile). When the entries reaching the
  changed lines are few, the selector re-runs just those on the patched code in a
  forked child, and drops the test if every digest matches. Lines the test runs
  outside the entries, or at module level, rule this out. A cheaper staged form
  runs first (`map/tpcds_stages.json.gz`): when every changed line runs only in the
  entries' early stages (parsing, the executor's optimizer call, the DuckDB
  transpile) and never in pickling, schema and table setup, planning, execution or
  module-level code, it compares only those stages' outputs, since the later stages
  then receive identical input and run identical code.
- **`test_generator_memory`**: `map/genmem.json.gz` records which lines each
  dialect's subtest reaches; the selector re-runs only the affected dialects'
  generator loop and memory check on the patched code, and drops the test if they
  all pass.

The selected tests are split by longest-processing-time first, for whichever
worker count from 1 to 8 gives the lowest cost (busiest worker plus 2 s per
worker).

## Local evaluation

`tools/` holds the pipeline:

- `trace_all.py`, `build_map.py` and `build_static.py` build the maps.
- `mutate.py` generates synthetic patches, and `label.py` labels them by
  running the full suite.
- `evaluate.py` scores the selector on the sample and synthetic sets with the
  official `score.py`.

Sample set: 101.4 s simulated with 0 misses, against 1614.1 s for the starter.
Synthetic sets (399 labeled mutations of many kinds): 0 misses.

## Open-source software used

- **Runtime** (the submitted image): the `python:3.13-slim` Docker image and the
  Python standard library. The build downloads sqlglot (MIT) at the pinned
  commit `eb11aec`, whose source the selector compares patches against and whose
  TPC-DS fixtures and executor it runs to check digests.
- **Map building and local evaluation** (`tools/`, not in the image): sqlglot's
  own test suite and its locked dependencies (`tools/requirements.lock`), notably
  pytest, pytest-xdist and pytest-timeout, plus duckdb, numpy and pandas, which
  the suite needs. `tools/evaluate.py` uses the scorer shipped in the track's
  starter kit.

Everything else here (the selector, the maps and the tools) is our own work,
released under the MIT License (`LICENSE`).
