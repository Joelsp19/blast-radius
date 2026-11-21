"""Build-time: per fixture-item line coverage of tests that loop over SQL fixture files
(load_sql_fixture_pairs / load_sql_fixtures), for fixture-item slicing.

Each item is run in isolation (the loader yields only that item); "base" is the run with
no items (code outside the loop). A test is sliceable only if every isolated run passes.

usage: python trace_fixtures.py tests.json out.json [shard nshards]"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa

harness.setup_env()
PREFIX = harness.SG.rstrip("/") + "/"
mon = sys.monitoring
TOOL = 4
mon.use_tool_id(TOOL, "br-fix")
files, flist, cur, _fidx = {}, [], set(), {}
MODS = ("tests/test_optimizer.py", "tests/test_serde.py", "tests/test_transpile.py")


def on_line(code, line):
    fi = _fidx.get(code)
    if fi is None:
        fn = code.co_filename
        if fn.startswith(PREFIX) and fn[len(PREFIX):].startswith(("sqlglot/", "tests/")):
            rel = fn[len(PREFIX):]
            fi = files.get(rel)
            if fi is None:
                fi = files[rel] = len(flist)
                flist.append(rel)
        else:
            fi = -1
        _fidx[code] = fi
    if fi >= 0:
        cur.add((fi, line))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)


def traced(klass, meth, subset):
    cur.clear()
    mon.restart_events()
    mon.set_events(TOOL, mon.events.LINE)
    t0 = time.process_time()
    failed = harness.loader_run(klass, meth, subset)
    dt = time.process_time() - t0
    mon.set_events(TOOL, 0)
    return failed, set(cur), dt


def main():
    tests = [t["id"] for t in harness.load_tests_json(sys.argv[1]) if t["file"] in MODS]
    if len(sys.argv) > 3:
        k, n = int(sys.argv[3]), int(sys.argv[4])
        tests = tests[k::n]
    harness.preload(tests)
    res = {}
    for tid in tests:
        klass, meth = harness.get_class(tid)
        if klass is None:
            continue
        t0 = time.process_time()
        failed = harness.loader_run(klass, meth, None)
        full_cpu = time.process_time() - t0
        n = harness._LOADER["n"]
        if failed or n < 2:
            continue
        bf, base, bcpu = traced(klass, meth, set())
        if bf:
            continue
        lines, cpus, ok = [], [], True
        for i in range(n):
            f, ln, dt = traced(klass, meth, {i})
            if f:
                ok = False
                break
            lines.append(sorted(ln - base))
            cpus.append(round(dt, 4))
        if not ok:
            print("unsliceable", tid, file=sys.stderr)
            continue
        res[tid] = {"attr": "__loader__", "lines": lines, "cpu": cpus, "base": sorted(base),
                    "base_cpu": round(bcpu, 4), "full_cpu": round(full_cpu, 3)}
        print(tid, n, round(full_cpu, 2), round(sum(cpus), 2), file=sys.stderr, flush=True)
    with open(sys.argv[2], "w") as f:
        json.dump({"files": flist, "subtests": res}, f)


if __name__ == "__main__":
    main()
