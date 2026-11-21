"""Run tests of one file, each in a forked child so each gets its own record.
usage: run_one.py SRC OUTDIR TESTS_JSON FILE   (one process per test file; a fresh
interpreter per test: each test is exec'd via this script with --one)"""
import concurrent.futures as cf, json, os, pickle, sys, unittest


class SyncPool:
    """In-process stand-in for ProcessPoolExecutor: same calls, pickled both ways."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def shutdown(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:
            f.set_exception(e)
        return f


def main():
    src, test_id = sys.argv[1], sys.argv[2]
    os.chdir(src)
    sys.path.insert(0, src)
    file, cls, meth = test_id.split("::")
    import importlib
    mod = importlib.import_module(file[:-3].replace("/", "."))
    if hasattr(mod, "ProcessPoolExecutor"):
        mod.ProcessPoolExecutor = SyncPool
    suite = unittest.TestSuite([getattr(mod, cls)(meth)])
    r = unittest.TestResult()
    suite.run(r)
    ok = r.wasSuccessful()
    sys.stdout.write("OK\n" if ok else "FAIL " + ((r.failures + r.errors)[0][1][-400:].replace("\n", " | ")) + "\n")


if __name__ == "__main__":
    main()
