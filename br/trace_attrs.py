"""Build-time attribute-read coverage: per test, the attribute / global names that sqlglot
or test code actually loaded (LOAD_ATTR, LOAD_GLOBAL, ...), via sys.monitoring INSTRUCTION
events disabled after the first hit per location. Sharper than line coverage: it sees
short-circuits such as `is_bytes and "x" in cls.NUMERIC_ESCAPES`.

usage: python trace_attrs.py tests.json out.json"""

import dis
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa

harness.setup_env()
PREFIX = harness.SG.rstrip("/") + "/"
mon = sys.monitoring
TOOL = 4
mon.use_tool_id(TOOL, "br-attrs")
OPS = {"LOAD_ATTR", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_SUPER_ATTR", "LOAD_FROM_DICT_OR_GLOBALS",
       "LOAD_METHOD"}
_offs = {}
names = {}
nlist = []
cur = set()


def offsets(code):
    o = _offs.get(code)
    if o is None:
        o = {}
        if code.co_filename.startswith(PREFIX):
            for ins in dis.get_instructions(code):
                if ins.opname in OPS and isinstance(ins.argval, str):
                    i = names.get(ins.argval)
                    if i is None:
                        i = names[ins.argval] = len(nlist)
                        nlist.append(ins.argval)
                    o[ins.offset] = i
        _offs[code] = o
    return o


def on_ins(code, offset):
    i = offsets(code).get(offset)
    if i is not None:
        cur.add(i)
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.INSTRUCTION, on_ins)


def main():
    tests = harness.load_tests_json(sys.argv[1])
    harness.preload([t["id"] for t in tests])
    res = {}
    for t in tests:
        cur.clear()
        mon.restart_events()
        mon.set_events(TOOL, mon.events.INSTRUCTION)
        harness.run_id(t["id"])
        mon.set_events(TOOL, 0)
        res[t["id"]] = sorted(cur)
    with open(sys.argv[2], "w") as f:
        json.dump({"names": nlist, "tests": res}, f)
    print("done", len(nlist), file=sys.stderr)


if __name__ == "__main__":
    main()
