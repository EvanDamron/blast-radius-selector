"""Run every test in its own interpreter against the instrumented tree and merge the
per-test comparison records. usage: record_all.py INSTR_SRC TESTS_JSON OUTDIR JOBS"""
import concurrent.futures as cf, glob, json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def one(args):
    src, t, outdir = args
    out = f"{outdir}/rec.{t['number']}"
    env = dict(os.environ, BR_REC_OUT=out, PYTHONPATH=src, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    t0 = time.time()
    p = subprocess.run([sys.executable, str(HERE / "run_one.py"), src, t["id"]], env=env, capture_output=True, text=True, timeout=3600)
    merged = {}
    import gzip
    for f in glob.glob(out + ".*"):
        for k, v in json.load(gzip.open(f, "rt")).items():
            merged[k] = merged.get(k, 0) | v
        os.unlink(f)
    with gzip.open(f"{outdir}/t{t['number']}.json.gz", "wt", compresslevel=3) as fh:
        json.dump(merged, fh)
    last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "NOOUT " + p.stderr[-300:].replace("\n", " | ")
    return t["number"], last, time.time() - t0


def main():
    src, tj, outdir, jobs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    os.makedirs(outdir, exist_ok=True)
    tests = json.load(open(tj))["tests"]
    todo = [t for t in tests if not os.path.exists(f"{outdir}/t{t['number']}.json.gz")]
    # heavy first
    todo.sort(key=lambda t: -t["seconds"])
    if os.environ.get("BR_ORDER") == "asc":
        todo.reverse()
    if os.environ.get("BR_LIMIT"):
        todo = todo[: int(os.environ["BR_LIMIT"])]
    status = {}
    with cf.ThreadPoolExecutor(jobs) as ex:
        for n, last, dt in ex.map(one, [(src, t, outdir) for t in todo]):
            status[n] = last
            if not last.startswith("OK"):
                print("NOTOK", n, tests[n]["id"], last[:300], flush=True)
            if len(status) % 100 == 0:
                print("done", len(status), flush=True)
    json.dump(status, open(f"{outdir}/status.json", "w"))


if __name__ == "__main__":
    main()
