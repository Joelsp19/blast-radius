"""Build-time line coverage per test (sys.monitoring LINE events, disabled after first hit
per test). Also records preload line coverage.

usage: python trace_lines.py tests.json out.json"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa

harness.setup_env()
PREFIX = harness.SG.rstrip("/") + "/"
mon = sys.monitoring
TOOL = 4
mon.use_tool_id(TOOL, "br-lines")

files = {}
flist = []
cur = set()
_fidx = {}


def on_line(code, line):
    fi = _fidx.get(code)
    if fi is None:
        fn = code.co_filename
        if fn.startswith(PREFIX) and (fn[len(PREFIX):].startswith(("sqlglot/", "tests/"))):
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


def begin():
    cur.clear()
    mon.restart_events()
    mon.set_events(TOOL, mon.events.LINE)


def end():
    mon.set_events(TOOL, 0)
    return sorted(cur)


def main():
    tests = harness.load_tests_json(sys.argv[1])
    begin()
    harness.preload([t["id"] for t in tests])
    pre = end()
    res = {}
    for t in tests:
        begin()
        harness.run_id(t["id"])
        res[t["id"]] = end()
    with open(sys.argv[2], "w") as f:
        json.dump({"files": flist, "preload": pre, "tests": res}, f)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
