"""Build-time key-level coverage of class-level containers.

Every dict / set / frozenset held in a sqlglot class namespace is replaced by a recording
subclass with the same contents. Per test we record which keys were looked up
(get / [] / in) and which containers were observed as a whole (iteration, len, copy, ...).
A container that is also referenced from anywhere other than its class namespace (so
reads could bypass the recorder) is marked unsafe.

usage: python trace_keys.py tests.json out.json"""

import gc
import hashlib
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa
import statediff  # noqa

harness.setup_env()

CUR_KEYS = set()
CUR_WHOLE = set()


def kfp(k):
    return hashlib.blake2b(statediff._fp(k, 2, set()).encode(), digest_size=6).hexdigest()


SUPPRESS = [0]


def _key(label, k):
    if SUPPRESS[0]:
        return
    try:
        CUR_KEYS.add((label, kfp(k)))
    except Exception:
        CUR_WHOLE.add(label)


def _whole_methods(base, names):
    ns = {}
    for n in names:
        orig = getattr(base, n, None)
        if orig is None:
            continue

        def mk(orig):
            def f(self, *a, **k):
                if not SUPPRESS[0]:
                    CUR_WHOLE.add(self._br_label)
                return orig(self, *a, **k)

            return f

        ns[n] = mk(orig)
    return ns


WHOLE_DICT = ("__iter__", "keys", "items", "values", "__len__", "__eq__", "__ne__", "copy",
              "__or__", "__ror__", "__ior__", "__reversed__", "__repr__", "__str__", "popitem",
              "__reduce__", "__reduce_ex__", "update", "__setitem__", "__delitem__", "pop",
              "setdefault", "clear", "__format__", "__sizeof__")
WHOLE_SET = ("__iter__", "__len__", "__eq__", "__ne__", "copy", "__or__", "__ror__", "__and__",
             "__rand__", "__sub__", "__rsub__", "__xor__", "__rxor__", "__le__", "__lt__",
             "__ge__", "__gt__", "union", "intersection", "difference", "symmetric_difference",
             "issubset", "issuperset", "isdisjoint", "__repr__", "__str__", "__reduce__",
             "__reduce_ex__", "__hash__", "add", "discard", "remove", "pop", "update", "clear",
             "__ior__", "__iand__", "__isub__", "__ixor__", "__format__", "__sizeof__",
             "difference_update", "intersection_update", "symmetric_difference_update")


def _dict_ns():
    ns = _whole_methods(dict, WHOLE_DICT)

    def __getitem__(self, k):
        _key(self._br_label, k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        _key(self._br_label, k)
        return dict.get(self, k, d)

    def __contains__(self, k):
        _key(self._br_label, k)
        return dict.__contains__(self, k)

    ns.update(__getitem__=__getitem__, get=get, __contains__=__contains__)
    return ns


def _set_ns(base):
    ns = _whole_methods(base, WHOLE_SET)

    def __contains__(self, k):
        _key(self._br_label, k)
        return base.__contains__(self, k)

    ns["__contains__"] = __contains__
    return ns


RDict = type("RDict", (dict,), _dict_ns())
RSet = type("RSet", (set,), _set_ns(set))
RFrozen = type("RFrozen", (frozenset,), _set_ns(frozenset))
RDict.__hash__ = None


def wrap_all():
    labels = {}
    unsafe = set()
    pre = harness.SG.rstrip("/") + "/"
    classes = []
    for mn, mod in list(sys.modules.items()):
        if not (mn == "sqlglot" or mn.startswith("sqlglot.")) or mod is None:
            continue
        for v in list(vars(mod).values()):
            if isinstance(v, type) and v.__module__ == mn:
                classes.append(v)
    seen = set()
    todo = list(classes)
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        try:
            todo.extend(type.__subclasses__(c))
        except TypeError:
            pass
        todo.extend(v for v in vars(c).values() if isinstance(v, type))
    originals = []
    for c in seen:
        mod = c.__dict__.get("__module__") if isinstance(c.__dict__.get("__module__"), str) else ""
        if not mod.startswith("sqlglot"):
            continue
        for name, v in list(vars(c).items()):
            t = type(v)
            if t not in (dict, set, frozenset):
                continue
            label = f"{mod}:{c.__qualname__}.{name}"
            if t is dict:
                w = RDict(v)
            elif t is set:
                w = RSet(v)
            else:
                w = RFrozen(v)
            w._br_label = label
            originals.append((label, v, c))
            try:
                setattr(c, name, w)
            except (TypeError, AttributeError):
                continue
            labels[label] = len(v)
    # any other holder of an original container could read it without being recorded
    gc.collect()
    for label, v, c in originals:
        for r in gc.get_referrers(v):
            if r is originals or isinstance(r, tuple) and len(r) == 3 and r[1] is v:
                continue
            if isinstance(r, types.FrameType):
                continue
            unsafe.add(label)
            break
    # read by C-level set operations at run time (bypass the recorder)
    for label in labels:
        if label.rsplit(".", 1)[-1] in C_SET_OPS:
            unsafe.add(label)
    return labels, unsafe, seen


C_SET_OPS = ("SUPPORTED_SETTINGS",)


def patch_dispatch():
    """Generator dispatch tables are derived from TRANSFORMS (and cached): record lookups
    in them as key lookups of the TRANSFORMS dict they were built from."""
    import sqlglot.generator as g

    orig = g._build_dispatch

    def build(cls):
        T = cls.TRANSFORMS
        SUPPRESS[0] += 1
        try:
            d = orig(cls)
        finally:
            SUPPRESS[0] -= 1
        label = getattr(T, "_br_label", None)
        if label is None:
            return d
        w = RDict(d)
        w._br_label = label
        return w

    g._build_dispatch = build


def snapshot(classes):
    """State after preload: module-level containers and class namespaces."""
    mods = []
    for mn, mod in list(sys.modules.items()):
        if not (mn == "sqlglot" or mn.startswith("sqlglot.")) or mod is None:
            continue
        for name, v in list(vars(mod).items()):
            if type(v) in (dict, set, list):
                mods.append((v, type(v)(v)))
    cls = [(c, dict(vars(c))) for c in classes]
    return mods, cls


def restore(snap):
    """Reset lazily built caches so that every test rebuilds (and records) what it uses."""
    mods, cls = snap
    for obj, copy in mods:
        if isinstance(obj, list):
            obj[:] = copy
        else:
            obj.clear()
            obj.update(copy)
    for c, ns in cls:
        cur = vars(c)
        for name in [n for n in cur if n not in ns]:
            try:
                delattr(c, name)
            except (AttributeError, TypeError):
                pass
        for name, v in ns.items():
            if cur.get(name, None) is not v and name not in ("__dict__", "__weakref__"):
                try:
                    setattr(c, name, v)
                except (AttributeError, TypeError):
                    pass


def main():
    tests = harness.load_tests_json(sys.argv[1])
    if len(sys.argv) > 3:
        k, n = int(sys.argv[3]), int(sys.argv[4])
        tests = tests[k::n]
    harness.preload([t["id"] for t in tests])
    labels, unsafe, classes = wrap_all()
    patch_dispatch()
    snap = snapshot(classes)
    print("wrapped", len(labels), "unsafe", len(unsafe), file=sys.stderr, flush=True)
    res = {}
    for i, t in enumerate(tests):
        restore(snap)
        CUR_KEYS.clear()
        CUR_WHOLE.clear()
        failed = harness.run_id(t["id"])
        res[t["id"]] = {"k": sorted(CUR_KEYS), "w": sorted(CUR_WHOLE), "f": failed}
        if i % 100 == 0:
            print(i, file=sys.stderr, flush=True)
    with open(sys.argv[2], "w") as f:
        json.dump({"labels": labels, "unsafe": sorted(unsafe), "tests": res}, f)


if __name__ == "__main__":
    main()
