"""pytest plugin: write {nodeid: "fail"|"pass"} plus collection errors to $BRLABEL_OUT.

A test fails on any failed/errored report (setup, call, teardown or subtest).
"""
import json
import os

_status = {}
_collect_errors = []


def pytest_runtest_logreport(report):
    nodeid = report.nodeid.split("[")[0] if False else report.nodeid
    if report.failed:
        _status[nodeid] = "fail"
    else:
        _status.setdefault(nodeid, "pass")


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.append(report.nodeid)
        import sys; print("COLLECT ERROR", report.nodeid, str(report.longrepr)[-1500:], file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BRLABEL_OUT")
    if not out:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"{out}.{worker}.json", "w") as f:
        json.dump({"status": _status, "collect_errors": _collect_errors}, f)
