"""Build-time recorder (runs only against the pinned commit).

Three kinds of record, all per process (one test per process):
- comparisons: c(site, op, a, b) returns the original result and notes which other
  comparison operators would have produced a different result on the same operands.
  mask bit 0: the site executed. bit 1+k: operator k would have differed at least once.
- attribute reads: a(obj, name) / g(obj, name[, default]) / h(obj, name) return what
  getattr / hasattr return and note which class's definition the read resolved to.
- dict reads: d(site, value) wraps a class-level dict literal in a dict subclass that
  notes which keys were looked up, tested for membership, or read wholesale.
"""
import atexit, enum, json, os, types
from operator import eq, ne, lt, le, gt, ge, is_, is_not

OPS = (eq, ne, lt, le, gt, ge, is_, is_not)
N = len(OPS)
ALL = (1 << (N + 1)) - 1
_SAFE = (int, float, str, bool, tuple, type(None), bytes, frozenset)
data = {}
_busy = False


def c(site, op, a, b):
    global _busy
    try:
        res = OPS[op](a, b)
    except BaseException:
        # The original comparison raised here: any other operator may behave differently.
        data[site] = ALL
        raise
    m = data.get(site, 0)
    if m == ALL or _busy:
        if not m:
            data[site] = 1
        return res
    _busy = True
    try:
        new = m | 1
        if op >= 2 and op <= 5:
            alts = range(N)
        elif type(a) in _SAFE and type(b) in _SAFE:
            alts = range(N)
        else:
            alts = (0, 1, 6, 7)
            new |= 0b111100 << 1  # ordering operators not evaluated here: assume they differ
        for k in alts:
            bit = 1 << (k + 1)
            if k == op or new & bit:
                continue
            try:
                alt = OPS[k](a, b)
            except BaseException:
                new |= bit
                continue
            if alt is not res:
                if not (type(alt) is bool and type(res) is bool) or alt != res:
                    new |= bit
        if new != m:
            data[site] = new
    finally:
        _busy = False
    return res


# ------------------------------------------------------------- attribute reads

_owner = {}
_MODULE = types.ModuleType
_MISSING = object()


def _note(obj, name):
    try:
        t = type(obj)
        if t is _MODULE:
            return
        skip = None
        if t is super:
            skip = obj.__thisclass__
            t = obj.__self_class__
        elif isinstance(obj, type):
            t = obj
        else:
            try:
                dct = object.__getattribute__(obj, "__dict__")
            except AttributeError:
                dct = None
            if dct is not None and name in dct:
                return
        key = (t, skip, name)
        own = _owner.get(key)
        if own is None:
            own = ""
            mro = t.__mro__
            if skip is not None:
                mro = mro[mro.index(skip) + 1 :]
            for k in mro:
                if name in k.__dict__:
                    own = f"A|{k.__module__}:{k.__qualname__}.{name}"
                    break
            _owner[key] = own
        if own:
            data[own] = 1
    except Exception:
        pass


def a(obj, name):
    val = getattr(obj, name)
    _note(obj, name)
    return val


def g(obj, name, *default):
    val = getattr(obj, name, *default)
    if type(name) is str:
        _note(obj, name)
    return val


def h(obj, name):
    val = hasattr(obj, name)
    if type(name) is str:
        _note(obj, name)
    return val


# ------------------------------------------------------------------ dict reads


def _ck(k):
    t = type(k)
    if t is str:
        return "s" + k
    if t is int or t is bool or k is None:
        return repr(k)
    if isinstance(k, type):
        return "c" + k.__name__
    if isinstance(k, enum.Enum):
        return f"e{t.__name__}.{k.name}"
    return "?"


class TD(dict):
    __slots__ = ("_s",)

    def _v(self, k):
        data[f"D|{self._s}|v|{_ck(k)}"] = 1

    def _e(self, kind):
        data[f"D|{self._s}|{kind}"] = 1

    def __getitem__(self, k):
        self._v(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._v(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        data[f"D|{self._s}|h|{_ck(k)}"] = 1
        return dict.__contains__(self, k)

    def __iter__(self):
        self._e("k")
        return dict.__iter__(self)

    def __reversed__(self):
        self._e("k")
        return dict.__reversed__(self)

    def keys(self):
        self._e("k")
        return dict.keys(self)

    def __len__(self):
        self._e("l")
        return dict.__len__(self)

    def values(self):
        self._e("a")
        return dict.values(self)

    def items(self):
        self._e("a")
        return dict.items(self)

    def copy(self):
        self._e("a")
        return dict(dict.items(self))

    __copy__ = copy

    def __deepcopy__(self, memo):
        import copy as _copy

        self._e("a")
        return _copy.deepcopy(dict(dict.items(self)), memo)

    def __reduce__(self):
        self._e("a")
        return (dict, (dict(dict.items(self)),))

    def __eq__(self, other):
        self._e("a")
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._e("a")
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        self._e("a")
        return dict.__repr__(self)

    def __or__(self, other):
        self._e("a")
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        self._e("a")
        return other | dict(dict.items(self))

    def __ior__(self, other):
        self._e("a")
        dict.update(self, other)
        return self

    def __setitem__(self, k, v):
        self._e("a")
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._e("a")
        dict.__delitem__(self, k)

    def update(self, *a, **k):
        self._e("a")
        dict.update(self, *a, **k)

    def pop(self, *a):
        self._e("a")
        return dict.pop(self, *a)

    def popitem(self):
        self._e("a")
        return dict.popitem(self)

    def setdefault(self, *a):
        self._e("a")
        return dict.setdefault(self, *a)

    def clear(self):
        self._e("a")
        dict.clear(self)


KEYS = {}  # site -> canonical key of each explicit entry, in source order (keys run only)


def d(site, value, keys=None):
    if type(value) is not dict:
        return value
    out = TD(value)
    out._s = site
    data[f"D|{site}|c"] = 1
    if keys is not None and site not in KEYS:
        KEYS[site] = [_ck(k) for k in keys]
    return out


def _dump():
    out = os.environ.get("BR_REC_OUT")
    if out and data:
        import gzip
        with gzip.open(f"{out}.{os.getpid()}", "wt", compresslevel=3) as fh:
            json.dump(data, fh)


atexit.register(_dump)
