"""Build-time: per top-level statement line coverage of test methods (statement slicing).

Lines executed while a droppable statement (see stmtslice) runs are attributed to it;
everything else (non-droppable statements, setUp, ...) is "base", which always runs.

usage: python trace_stmts.py tests.json out.json [shard nshards]"""

import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa
import stmtslice  # noqa

harness.setup_env()
PREFIX = harness.SG.rstrip("/") + "/"
mon = sys.monitoring
TOOL = 4
mon.use_tool_id(TOOL, "br-stmt")
files, flist, _fidx = {}, [], {}
ST = {"code": None, "ranges": [], "cur": -1, "t": 0.0, "sets": {}, "cpu": {}}


def fidx(code):
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
    return fi


def stmt_of(line):
    for i, lo, hi, d in ST["ranges"]:
        if lo <= line <= hi:
            return i if d else -1
    return -1


def switch(idx):
    now = time.process_time()
    c = ST["cur"]
    ST["cpu"][c] = ST["cpu"].get(c, 0.0) + now - ST["t"]
    ST["t"] = now
    ST["cur"] = idx
    mon.restart_events()


def on_line(code, line):
    if code is ST["code"]:
        idx = stmt_of(line)
        if idx != ST["cur"]:
            switch(idx)
        return None
    fi = fidx(code)
    if fi >= 0:
        ST["sets"].setdefault(ST["cur"], set()).add((fi, line))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)


def main():
    tests = harness.load_tests_json(sys.argv[1])
    if len(sys.argv) > 3:
        k, n = int(sys.argv[3]), int(sys.argv[4])
        tests = tests[k::n]
    harness.preload([t["id"] for t in tests])
    snap = harness.snapshot_state()
    res = {}
    srcs = {}
    for t in tests:
        file, mod, cls, meth = harness.split_id(t["id"])
        klass, _ = harness.get_class(t["id"])
        if klass is None or meth not in klass.__dict__:
            continue
        p = os.path.join(harness.SG, file)
        if p not in srcs:
            srcs[p] = open(p, encoding="utf-8").read()
        try:
            info = stmtslice.analyze(srcs[p], cls, meth)
        except SyntaxError:
            info = None
        if not info or sum(1 for x in info if x[3]) < 4:
            continue
        fn = inspect.unwrap(klass.__dict__[meth])
        harness.restore_state(snap)
        ST.update(code=fn.__code__, ranges=info, cur=-1, sets={}, cpu={}, t=time.process_time())
        mon.restart_events()
        mon.set_events(TOOL, mon.events.LINE)
        t0 = time.process_time()
        failed = harness.run_id(t["id"])
        switch(-1)
        mon.set_events(TOOL, 0)
        full = time.process_time() - t0
        if failed:
            continue
        drop = [x[0] for x in info if x[3]]
        res[t["id"]] = {
            "attr": "__stmts__",
            "lines": [sorted(ST["sets"].get(i, ())) for i in range(len(info))],
            "cpu": [round(ST["cpu"].get(i, 0.0), 4) for i in range(len(info))],
            "droppable": drop,
            "base": sorted(ST["sets"].get(-1, ())),
            "base_cpu": round(ST["cpu"].get(-1, 0.0), 4),
            "full_cpu": round(full, 3),
        }
    with open(sys.argv[2], "w") as f:
        json.dump({"files": flist, "subtests": res}, f)
    print("done", len(res), file=sys.stderr)


if __name__ == "__main__":
    main()
