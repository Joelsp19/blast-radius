"""Build-time: enum classes in sqlglot, and which files each test module opens while being
imported / class-set-up (so fixture edits read at preload can be attributed).

usage: python extras.py tests.json out.json"""

import enum
import json
import os
import pkgutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa

harness.setup_env()
PREFIX = harness.SG.rstrip("/") + "/"
opened = set()


def audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes)):
            p = os.fsdecode(p)
            if p.startswith(PREFIX):
                opened.add(p[len(PREFIX):])


sys.addaudithook(audit)


def main():
    tests = harness.load_tests_json(sys.argv[1])
    import sqlglot

    harness.preload([], dialects=True, setups=False)
    for mi in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            __import__(mi.name)
        except Exception as e:
            print("skip", mi.name, e, file=sys.stderr)
    enums = []
    seen = set()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot"):
            continue
        for v in list(vars(mod).values()):
            if isinstance(v, type) and issubclass(v, enum.Enum) and v.__module__.startswith("sqlglot"):
                k = f"{v.__module__}:{v.__qualname__}"
                if k not in seen:
                    seen.add(k)
                    enums.append(k)
    by_mod = {}
    for t in tests:
        file, mod, cls, meth = harness.split_id(t["id"])
        by_mod.setdefault(mod, []).append(t["id"])
    pou = {}
    for mod, ids in by_mod.items():
        opened.clear()
        for tid in ids:
            klass, _ = harness.get_class(tid)
            if klass is not None:
                harness.class_setup(klass)
        for p in opened:
            pou.setdefault(p, []).extend(ids)
    with open(sys.argv[2], "w") as f:
        json.dump({"enums": enums, "preload_open_users": pou}, f)
    print("enums", len(enums), "pou", {k: len(v) for k, v in pou.items()}, file=sys.stderr)


if __name__ == "__main__":
    main()
