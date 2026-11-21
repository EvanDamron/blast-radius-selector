"""Which parts of a SQL fixture file a test reads, for map/fixtures.json.gz.

Active only when BRPOISON is set to "<file relative to tests/fixtures>|<kind>". The
values that tests/helpers.py yields for that file are replaced by an object that
fails on any use: kind "meta", "sql" or "expected" for load_sql_fixture_pairs, and
"entry" for load_sql_fixtures. Any use also touches BRPOISON_MARK, and so does an
open of the file from anywhere but those helpers. A test that passes without the
mark never read that part of the file.
"""
import os
import sys

_spec = os.environ.get("BRPOISON")
if _spec:
    import builtins

    _file, _kind = _spec.split("|")
    _mark = os.environ["BRPOISON_MARK"]

    def _touch(why):
        # The first use settles the verdict: record it and stop, since a poisoned value
        # reaching a process pool would otherwise deadlock it.
        try:
            with open(_mark, "a") as fh:
                fh.write(why + "\n")
        finally:
            os._exit(3)

    class PoisonUsed(BaseException):
        pass

    class Poison:
        __slots__ = ()

        def __getattribute__(self, name):
            _touch("attr " + name)
            raise PoisonUsed(name)

    def _dunder(name):
        def f(self, *args, **kwargs):
            _touch("dunder " + name)
            raise PoisonUsed(name)
        return f

    for _n in ("__len__", "__iter__", "__getitem__", "__contains__", "__eq__", "__ne__", "__lt__", "__le__",
               "__gt__", "__ge__", "__hash__", "__bool__", "__str__", "__repr__", "__format__", "__add__",
               "__radd__", "__mod__", "__rmod__", "__mul__", "__reduce__", "__reduce_ex__", "__getstate__",
               "__copy__", "__deepcopy__", "__fspath__", "__index__", "__int__", "__float__", "__call__"):
        setattr(Poison, _n, _dunder(_n))

    _helpers = {"load_sql_fixture_pairs", "load_sql_fixtures", "_filter_comments", "_extract_meta"}

    def _patch(mod):
        pairs, fixtures = mod.load_sql_fixture_pairs, mod.load_sql_fixtures

        def load_sql_fixture_pairs(filename):
            for meta, sql, expected in pairs(filename):
                if os.path.normpath(filename) == _file:
                    if _kind == "meta":
                        meta = Poison()
                    elif _kind == "sql":
                        sql = Poison()
                    elif _kind == "expected":
                        expected = Poison()
                yield meta, sql, expected

        def load_sql_fixtures(filename):
            for entry in fixtures(filename):
                if _kind == "entry" and os.path.normpath(filename) == _file:
                    entry = Poison()
                yield entry

        mod.load_sql_fixture_pairs = load_sql_fixture_pairs
        mod.load_sql_fixtures = load_sql_fixtures

    _orig_import = builtins.__import__
    _done = []

    def _import(name, *args, **kwargs):
        m = _orig_import(name, *args, **kwargs)
        if not _done:
            h = sys.modules.get("tests.helpers")
            if h is not None and hasattr(h, "load_sql_fixtures"):
                _done.append(1)
                _patch(h)
        return m

    builtins.__import__ = _import

    def _audit(event, args):
        if event == "open" and args and isinstance(args[0], str) and args[0].endswith(_file):
            f = sys._getframe(1)
            while f is not None:
                if f.f_code.co_name in _helpers:
                    return
                f = f.f_back
            _touch("direct open")

    sys.addaudithook(_audit)
