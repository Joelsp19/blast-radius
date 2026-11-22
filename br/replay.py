"""Differential replay of a few constructors that the executor's heavy tests call.

Build time (record): with the pinned code, every call to the REPLAYED constructors made while
the heavy TPC tests run is recorded (arguments pickled, deduplicated).

Run time (prove): a commit that changes only REPLAYED functions is replayed: for every
recorded call, the old and the new function run on independent copies of the arguments, and
the resulting object state, argument state and exception must match. If all calls match (and
the import-time and class-setup state are unchanged, checked separately), the heavy tests
execute exactly as at the pinned commit, where they pass: by induction every later call sees
the same inputs.
"""

import hashlib
import os
import pickle
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAYED = {
    "sqlglot.executor.table": ("Table", "RowReader", "RangeReader"),
}
HEAVY = (
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
)


class _Gen:
    """Marker for a generator argument: replayed as a fresh generator over its items."""

    def __init__(self, items):
        self.items = items


def _freeze(x):
    return _Gen(tuple(x)) if isinstance(x, types.GeneratorType) else x


def _thaw(x):
    return (y for y in x.items) if isinstance(x, _Gen) else x


def path_for(mod, cls):
    return os.path.join(HERE, f"replay_{mod.replace('.', '_')}_{cls}.pkl")


def record():
    """Build time: run the heavy tests with recording constructors; one file per class."""
    sys.path.insert(0, HERE)
    import harness

    harness.setup_env()
    harness.preload(list(HEAVY))
    k, _ = harness.get_class(HEAVY[0])
    harness.class_setup(k)
    getattr(k, "tpch_sqls")  # build the (lazy) class setup before recording
    import importlib

    store = {}
    seen = set()
    bad = set()
    for mod, classes in REPLAYED.items():
        m = importlib.import_module(mod)
        for cname in classes:
            cls = getattr(m, cname)
            orig = cls.__init__
            key = (mod, cname)
            store[key] = []

            def mk(key, orig):
                def rec(self, *a, **kw):
                    a2 = tuple(_freeze(x) for x in a)
                    kw2 = {n: _freeze(v) for n, v in kw.items()}
                    try:
                        b = pickle.dumps((a2, kw2), protocol=5)
                    except Exception:  # noqa
                        bad.add(key)
                        b = None
                    if b is not None:
                        h = hashlib.blake2b(b, digest_size=16).digest()
                        if (key, h) not in seen:
                            seen.add((key, h))
                            store[key].append(b)
                    return orig(self, *(_thaw(x) for x in a2), **{n: _thaw(v) for n, v in kw2.items()})

                return rec

            cls.__init__ = mk(key, orig)
    failed = [t for t in HEAVY if harness.run_id(t)]
    for (mod, cname), calls in store.items():
        p = path_for(mod, cname)
        if failed or (mod, cname) in bad:
            if os.path.exists(p):
                os.remove(p)
            continue
        with open(p, "wb") as f:
            pickle.dump(calls, f, protocol=5)
    print("replay recorded", {f"{m}.{c}": len(v) for (m, c), v in store.items()}, "failed", failed,
          "unpicklable", sorted(bad), file=sys.stderr)


def _eq(a, b, seen, depth=0):
    if depth > 40:
        return False
    if type(a) is not type(b):
        return False
    if a is None or isinstance(a, (bool, int, float, str, bytes, range)):
        return a == b or (a != a and b != b)  # NaN
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        try:
            if a == b:
                return True
        except Exception:  # noqa
            pass
        return all(_eq(x, y, seen, depth + 1) for x, y in zip(a, b))
    if isinstance(a, dict):
        if list(a) != list(b):
            return False
        return all(_eq(a[k], b[k], seen, depth + 1) for k in a)
    if isinstance(a, _Gen):
        return _eq(a.items, b.items, seen, depth + 1)
    key = (id(a), id(b))
    if key in seen:
        return True
    seen.add(key)
    if hasattr(a, "__dict__") and type(a).__module__.startswith("sqlglot"):
        return _eq(dict(vars(a)), dict(vars(b)), seen, depth + 1)
    if isinstance(a, BaseException):
        return repr(a) == repr(b)
    try:
        return bool(a == b)
    except Exception:  # noqa
        return False


def prove(swaps_by_path, log=None):
    """swaps_by_path: [(relpath, [(old_code, new_code, own, parent), ...])]. True if every
    changed function is a replayed __init__ and behaves identically on all recorded calls."""
    import importlib

    targets = []
    for path, swaps in swaps_by_path:
        mod = path[:-3].replace("/", ".")
        if mod not in REPLAYED:
            return False
        for oc, nc, own, parent in swaps:
            parts = oc.co_qualname.split(".")
            if len(parts) != 2 or parts[1] != "__init__" or parts[0] not in REPLAYED[mod]:
                return False
            if oc.co_freevars or nc.co_freevars:
                return False
            targets.append((mod, parts[0], oc))
    if not targets:
        return False
    for mod, cname, oc in targets:
        p = path_for(mod, cname)
        if not os.path.exists(p):
            return False
        m = importlib.import_module(mod)
        cls = getattr(m, cname)
        new = cls.__dict__.get("__init__")
        if not isinstance(new, types.FunctionType):
            return False
        old = types.FunctionType(oc, new.__globals__, "__init__", new.__defaults__, None)
        old.__kwdefaults__ = new.__kwdefaults__
        with open(p, "rb") as f:
            calls = pickle.load(f)
        for b in calls:
            outs = []
            for fn in (old, new):
                a, kw = pickle.loads(b)
                obj = cls.__new__(cls)
                try:
                    fn(obj, *(_thaw(x) for x in a), **{n: _thaw(v) for n, v in kw.items()})
                    exc = None
                except BaseException as e:  # noqa
                    exc = e
                outs.append((obj, a, kw, exc))
            (o1, a1, k1, e1), (o2, a2, k2, e2) = outs
            if (e1 is None) != (e2 is None) or (e1 is not None and repr(e1) != repr(e2)):
                if log:
                    log("replay: exception differs", cname)
                return False
            if not _eq(dict(vars(o1)), dict(vars(o2)), set()) or not _eq((a1, k1), (a2, k2), set()):
                if log:
                    log("replay: state differs", cname)
                return False
    return True




# ---------------------------------------------------------------- edited heavy test methods

class _Sentinel:
    """Stand-in value: compared by label only."""

    def __init__(self, label):
        self.label = label


class _Proxy:
    """`self` for a stubbed test method: methods of the test class become recorders (calls
    are logged, a sentinel is returned); any other attribute is a sentinel named after it."""

    def __init__(self, klass, log):
        object.__setattr__(self, "_k", klass)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        klass, log = object.__getattribute__(self, "_k"), object.__getattribute__(self, "_log")
        if callable(getattr(klass, name, None)):
            def rec(*a, **kw):
                log.append((name, a, kw))
                return _Sentinel(f"ret:{name}:{len(log)}")

            return rec
        return _Sentinel(f"attr:{name}")

    def __setattr__(self, name, value):
        raise TypeError("stubbed test method assigns an attribute")


def _canon(x, depth=0):
    if depth > 20:
        raise ValueError("deep")
    if isinstance(x, _Sentinel):
        return ("S", x.label)
    if x is None or isinstance(x, (bool, int, float, str, bytes)):
        return (type(x).__name__, repr(x))
    if isinstance(x, (tuple, list)):
        return (type(x).__name__, tuple(_canon(y, depth + 1) for y in x))
    return ("id", id(x))  # module-level objects: the very same object in both runs


def test_method_unchanged(module, klass, oc, nc):
    """Run the old and the new body of a test method with `self` stubbed: True if both make
    exactly the same calls on self with the same arguments (then, everything it calls being
    unchanged, the real test runs exactly as before). Only bodies that read nothing but plain
    data globals and make no calls other than on self are accepted."""
    import builtins
    import dis

    def ok_code(code):
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                return False  # nested functions / comprehensions: not handled
        for ins in dis.get_instructions(code):
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
                name = ins.argval
                if name in module.__dict__:
                    v = module.__dict__[name]
                elif hasattr(builtins, name):
                    v = getattr(builtins, name)
                else:
                    return False
                if callable(v) or isinstance(v, types.ModuleType):
                    return False
            elif ins.opname in ("STORE_GLOBAL", "DELETE_GLOBAL", "IMPORT_NAME", "STORE_ATTR", "DELETE_ATTR"):
                return False
        return True

    if oc.co_freevars or nc.co_freevars or not ok_code(oc) or not ok_code(nc):
        return False
    logs = []
    for code in (oc, nc):
        log = []
        fn = types.FunctionType(code, module.__dict__, code.co_name)
        try:
            ret = fn(_Proxy(klass, log))
        except BaseException:  # noqa
            return False
        logs.append([(n, _canon(a), _canon(sorted(kw.items()))) for n, a, kw in log] + [("return", _canon(ret))])
    return logs[0] == logs[1]
