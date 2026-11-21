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
  which files each test opened and which expression classes each test
  instantiated.
- `static.json.gz`: every function definition by name, and the expression
  class tree.

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
  dialect imports keep this narrow.
- **Syntax errors**: every test that imports the module.
- **Fixtures and other data files**: the tests that opened the file.
- **pytest config**: everything, if the edit can reach pytest.

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

Sample set: 179.6 s simulated with 0 misses, against 1614.1 s for the starter.
