"""Build-time recorder of operand relations (runs only against the pinned commit).

Every instrumented comparison `a <op> b` calls c(site, op, a, b). It returns the
original result and records, for this process, the relation signature of the operands:
the truth value of each of the eight relations ==, !=, <, <=, >, >=, is, is not.
Each relation is one of T (true), F (false), X (raised or did not give a bool) or
N (not evaluated: ordering relations are only tried on plain values or where the
source itself orders the operands).
"""
import atexit, gzip, json, os
from operator import eq, ne, lt, le, gt, ge, is_, is_not

OPS = (eq, ne, lt, le, gt, ge, is_, is_not)
_SAFE = (int, float, str, bool, tuple, type(None), bytes, frozenset)
data = {}
_busy = False


def c(site, op, a, b):
    global _busy
    try:
        res = OPS[op](a, b)
    except BaseException:
        data[f"{site}|" + "X" * 8] = 1
        raise
    if _busy:
        return res
    _busy = True
    try:
        ordered = (2 <= op <= 5) or (type(a) in _SAFE and type(b) in _SAFE)
        sig = []
        for k in range(8):
            if k == op:
                v = res
            elif 2 <= k <= 5 and not ordered:
                sig.append("N")
                continue
            else:
                try:
                    v = OPS[k](a, b)
                except BaseException:
                    sig.append("X")
                    continue
            sig.append("T" if v is True else "F" if v is False else "X")
        data[f"{site}|{''.join(sig)}"] = 1
    finally:
        _busy = False
    return res


def _dump():
    out = os.environ.get("BR_REC_OUT")
    if out and data:
        with gzip.open(f"{out}.{os.getpid()}", "wt", compresslevel=3) as fh:
            json.dump(data, fh)


atexit.register(_dump)
