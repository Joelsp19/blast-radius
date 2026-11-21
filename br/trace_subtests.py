"""Build-time: line coverage per query of the TPC executor tests (sub-test slicing).

Each of these tests loops over independent queries; running only the queries that execute
a changed line is equivalent for deciding pass/fail of that change.

usage: python trace_subtests.py out.json"""

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
mon.use_tool_id(TOOL, "br-sub")
files, flist, cur, _fidx = {}, [], set(), {}


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

SUBTESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
}


def main():
    res = {}
    for tid, attr in SUBTESTS.items():
        harness.preload([tid])
        klass, meth = harness.get_class(tid)
        full = list(getattr(klass, attr))
        per_lines, per_cpu, per_fail = [], [], []
        for i in range(len(full)):
            setattr(klass, attr, [full[i]])
            cur.clear()
            mon.restart_events()
            mon.set_events(TOOL, mon.events.LINE)
            t0 = time.process_time()
            failed = harness.run_case(klass, meth)
            dt = time.process_time() - t0
            mon.set_events(TOOL, 0)
            per_lines.append(sorted(cur))
            per_cpu.append(round(dt, 3))
            per_fail.append(failed)
        setattr(klass, attr, full)
        res[tid] = {"attr": attr, "lines": per_lines, "cpu": per_cpu, "failed": per_fail}
        print(tid, len(full), "cpu", round(sum(per_cpu), 1), "failed", sum(per_fail), file=sys.stderr)
    with open(sys.argv[1], "w") as f:
        json.dump({"files": flist, "subtests": res}, f)


if __name__ == "__main__":
    main()
