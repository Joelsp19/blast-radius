"""Differential class-state comparison for class/module-body changes.

The parent (original code) fingerprints every attribute of the classes defined in the
changed modules and of all their subclasses; a forked child that imported the patched
code does the same. Attribute names whose fingerprints differ are the only channels
through which the change can be observed (including values derived by metaclasses at
class creation). Tests are then sliced to those executing a line that mentions one of
those names.
"""

import enum
import hashlib
import os
import re
import sys
import types

_PRIM = (int, float, complex, str, bytes, bool, type(None))


def _fp(v, depth, seen):
    if isinstance(v, _PRIM):
        return f"{type(v).__name__}:{v!r}"
    if isinstance(v, enum.Enum):
        return f"enum:{type(v).__qualname__}.{v.name}"
    if isinstance(v, type):
        return f"cls:{v.__module__}:{v.__qualname__}"
    if depth <= 0:
        return f"deep:{type(v).__name__}"
    i = id(v)
    if i in seen:
        return "cycle"
    seen.add(i)
    try:
        if isinstance(v, (types.FunctionType, types.MethodType)):
            f = v.__func__ if isinstance(v, types.MethodType) else v
            parts = [_code_fp(f.__code__)]
            if f.__defaults__:
                parts.append(_fp(f.__defaults__, depth - 1, seen))
            if f.__kwdefaults__:
                parts.append(_fp(f.__kwdefaults__, depth - 1, seen))
            if f.__closure__:
                for c in f.__closure__:
                    try:
                        parts.append(_fp(c.cell_contents, depth - 1, seen))
                    except ValueError:
                        parts.append("empty")
            return "fn:" + "|".join(parts)
        if isinstance(v, (staticmethod, classmethod)):
            return "sm:" + _fp(v.__func__, depth, seen)
        if isinstance(v, property):
            return "prop:" + "|".join(_fp(x, depth - 1, seen) for x in (v.fget, v.fset, v.fdel))
        if isinstance(v, dict):
            items = []
            for k, x in v.items():
                items.append(_fp(k, 2, seen) + "=" + _fp(x, depth - 1, seen))
            items.sort()
            return "dict{" + ",".join(items) + "}"
        if isinstance(v, (list, tuple)):
            return type(v).__name__ + "[" + ",".join(_fp(x, depth - 1, seen) for x in v) + "]"
        if isinstance(v, (set, frozenset)):
            return "set{" + ",".join(sorted(_fp(x, depth - 1, seen) for x in v)) + "}"
        if isinstance(v, types.ModuleType):
            return f"mod:{v.__name__}"
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            return f"obj:{type(v).__module__}.{type(v).__qualname__}:" + _fp(d, depth - 1, seen)
        try:
            r = repr(v)
        except Exception:
            r = "?"
        if " at 0x" in r:
            r = re.sub(r" at 0x[0-9a-f]+", "", r)
        return f"{type(v).__qualname__}:{r[:200]}"
    finally:
        seen.discard(i)


def _code_fp(code):
    import patchlib

    h = hashlib.blake2b(repr(patchlib.shallow_sig(code)).encode(), digest_size=8)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            h.update(_code_fp(c).encode())
    return h.hexdigest()


def _digest(s):
    return hashlib.blake2b(s.encode(), digest_size=12).hexdigest()


def class_keys(modnames):
    """Classes defined in the given modules (incl. nested) plus all their subclasses."""
    out = {}
    todo = []
    for mn in modnames:
        mod = sys.modules.get(mn)
        if mod is None:
            continue
        stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == mn]
        while stack:
            c = stack.pop()
            k = f"{c.__module__}:{c.__qualname__}"
            if k in out:
                continue
            out[k] = c
            todo.append(c)
            stack.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == mn)
    while todo:
        c = todo.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            continue
        for s in subs:
            k = f"{s.__module__}:{s.__qualname__}"
            if k not in out:
                out[k] = s
                todo.append(s)
    return out


def fingerprint(classes):
    """{classkey: {attr: digest}} of each class's own namespace."""
    res = {}
    for k, c in classes.items():
        attrs = {}
        for name, v in list(vars(c).items()):
            if name in ("__dict__", "__weakref__", "__doc__", "__firstlineno__", "__static_attributes__"):
                continue
            try:
                attrs[name] = _digest(_fp(v, 6, set()))
            except Exception:
                attrs[name] = "err"
        res[k] = attrs
    return res


def module_fingerprint(modnames):
    res = {}
    for mn in modnames:
        mod = sys.modules.get(mn)
        if mod is None:
            continue
        attrs = {}
        for name, v in list(vars(mod).items()):
            if name.startswith("__"):
                continue
            if isinstance(v, types.ModuleType):
                continue
            try:
                attrs[name] = _digest(_fp(v, 6, set()))
            except Exception:
                attrs[name] = "err"
        res[mn] = attrs
    return res


def changed_names(old, new):
    names = set()
    for k in set(old) | set(new):
        a, b = old.get(k), new.get(k)
        if a is None or b is None:
            # class appeared / disappeared: its name is the channel
            names.add(k.split(":", 1)[1].split(".")[-1])
            continue
        for n in set(a) | set(b):
            if a.get(n) != b.get(n):
                names.add(n)
    return names


_ident_cache = {}


def lines_mentioning(root, names, files):
    """{relpath: set(line numbers)} of lines containing any of `names` as an identifier."""
    if not names:
        return {}
    pat = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
    out = {}
    for rel in files:
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if not pat.search(text):
            continue
        hits = set()
        for i, ln in enumerate(text.split("\n"), 1):
            if pat.search(ln):
                hits.add(i)
        out[rel] = hits
    return out
