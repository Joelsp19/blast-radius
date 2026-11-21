"""Build-time tracer (run once, offline, at the pinned commit).

Phase 1 (preload) mirrors the runtime zygote: import sqlglot, every dialect, every test
module and run every setUpClass. Code executed here is recorded as "preload" coverage:
its effects are baked into the zygote, so changes to it cannot be hot-swapped.

Phase 2 runs each test and records:
  - code objects (functions/lambdas/genexprs) of sqlglot/ and tests/ it executed
  - sqlglot classes whose instances (self) or which classes (cls) ran any method
  - files under the repo it opened
  - its CPU time

usage: python trace_tests.py tests.json out.json
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa

harness.setup_env()
SG = harness.SG
PREFIX = SG.rstrip("/") + "/"

mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "br-trace")

keys = {}
key_list = []
current = set()
types_now = set()
_meth = {}  # code -> argname or None
_getframe = sys._getframe


def keyof(code):
    fn = code.co_filename
    if not fn.startswith(PREFIX):
        return None
    k = (fn[len(PREFIX):], code.co_qualname, code.co_firstlineno)
    i = keys.get(k)
    if i is None:
        i = keys[k] = len(key_list)
        key_list.append(k)
    return i


def on_start(code, offset):
    m = _meth.get(code, 0)
    if m == 0:
        i = keyof(code)
        if i is None:
            _meth[code] = None
            return mon.DISABLE
        m = None
        if code.co_argcount and code.co_varnames[0] in ("self", "cls"):
            m = code.co_varnames[0]
        _meth[code] = (i, m)
        current.add(i)
        if m is None:
            return mon.DISABLE
        i, m = _meth[code]
    elif m is None:
        return mon.DISABLE
    else:
        i, m = m
        current.add(i)
    try:
        obj = _getframe(1).f_locals[m]
    except Exception:
        return None
    types_now.add(obj if m == "cls" and isinstance(obj, type) else type(obj))
    return None


mon.register_callback(TOOL, mon.events.PY_START, on_start)

opened = set()


def audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes)):
            p = os.fsdecode(p)
            if p.startswith(PREFIX):
                opened.add(p[len(PREFIX):])


sys.addaudithook(audit)

all_types = set()


def begin():
    current.clear()
    opened.clear()
    types_now.clear()
    mon.restart_events()
    mon.set_events(TOOL, mon.events.PY_START)


def end():
    mon.set_events(TOOL, 0)
    all_types.update(types_now)
    return sorted(current), sorted(opened), types_now.copy()


def tkey(t):
    return f"{t.__module__}:{t.__qualname__}"


def main():
    import json

    tests = harness.load_tests_json(sys.argv[1])
    out = sys.argv[2]

    res = {"tests": {}, "opens": {}, "types": {}, "cpu": {}, "failed": [], "preload": None,
           "preload_opens": None, "import_errors": []}

    t0 = time.process_time()
    begin()
    harness.preload([t["id"] for t in tests])
    c, o, ty = end()
    res["preload"] = c
    res["preload_opens"] = o
    res["preload_cpu"] = time.process_time() - t0
    print("preload cpu", res["preload_cpu"], file=sys.stderr)

    snap = harness.snapshot_state()
    for t in tests:
        n = t["number"]
        harness.restore_state(snap)
        begin()
        t0 = time.process_time()
        failed = harness.run_id(t["id"])
        dt = time.process_time() - t0
        c, o, ty = end()
        res["tests"][n] = c
        res["opens"][n] = o
        res["types"][n] = sorted({tkey(x) for x in ty})
        res["cpu"][n] = round(dt, 4)
        if failed:
            res["failed"].append(n)
            print("FAILED", t["id"], file=sys.stderr)
    res["keys"] = key_list
    mro = {}
    for x in all_types:
        try:
            mro[tkey(x)] = [tkey(b) for b in x.__mro__]
        except Exception:
            pass
    res["mro"] = mro
    with open(out, "w") as f:
        json.dump(res, f)
    print("done", len(res["tests"]), "failed", len(res["failed"]), file=sys.stderr)


if __name__ == "__main__":
    main()
