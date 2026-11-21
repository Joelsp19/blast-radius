"""Statement-level slicing of test methods.

Many tests are long sequences of independent checks (self.validate_identity(...),
self.assertEqual(...), with self.assertRaises(...): ...). A top-level statement is
"droppable" when it is such a check: it binds no name that a later statement reads, sets
no attribute, and only calls self.validate*/self.assert* (or is a `with` over those whose
body is plain calls). At run time a test can be recompiled keeping all non-droppable
statements and just the droppable ones whose coverage reaches the change.
"""

import ast
import os
import types

CHECKS = ("validate", "assert", "check_")


def _is_check_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr.startswith(CHECKS)
    )


def _binds(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.NamedExpr):
            out.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
    return out


def _loads(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _sets_attr(node):
    return any(isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del))
               for n in ast.walk(node))


def _droppable_kind(st):
    if isinstance(st, ast.Expr) and _is_check_call(st.value):
        return True
    if isinstance(st, ast.With):
        if not all(_is_check_call(it.context_expr) for it in st.items):
            return False
        return all(isinstance(b, ast.Expr) and isinstance(b.value, ast.Call) for b in st.body)
    return False


def find_method(tree, cls, meth):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for f in node.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == meth:
                    return f
    return None


def analyze(src, cls, meth, tree=None):
    """[(index, lineno, end_lineno, droppable)] for the top-level statements, or None."""
    if tree is None:
        tree = ast.parse(src)
    f = find_method(tree, cls, meth)
    if f is None:
        return None
    for n in ast.walk(f):
        if isinstance(n, ast.Name) and n.id in ("super", "__class__"):
            return None
        if isinstance(n, (ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal)):
            return None
    body = f.body
    later_loads = [set() for _ in body]
    acc = set()
    for i in range(len(body) - 1, -1, -1):
        later_loads[i] = set(acc)
        acc |= _loads(body[i])
    out = []
    for i, st in enumerate(body):
        d = _droppable_kind(st) and not (_binds(st) & later_loads[i]) and not _sets_attr(st)
        out.append((i, st.lineno, st.end_lineno, d))
    return out


_cache = {}


def build(module, cls, meth, keep, root):
    """Function object for `cls.meth` with droppable statements outside `keep` removed."""
    import copy

    path = os.path.join(root, module.__name__.replace(".", "/") + ".py")
    c = _cache.get(path)
    if c is None:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        c = _cache[path] = (src, ast.parse(src), {})
    src, tree, infos = c
    f = copy.deepcopy(find_method(tree, cls, meth))
    info = infos.get((cls, meth))
    if info is None:
        info = infos[(cls, meth)] = analyze(src, cls, meth, tree)
    keep = set(keep)
    new_body = []
    for (i, lo, hi, d), st in zip(info, f.body):
        if d and i not in keep:
            continue
        new_body.append(st)
    if not new_body:
        new_body = [ast.Pass()]
    f.body = new_body
    mod = ast.Module(body=[f], type_ignores=[])
    ast.fix_missing_locations(mod)
    code = compile(mod, path, "exec")
    ns = {}
    exec(code, module.__dict__, ns)
    return ns[meth]


def run_sliced(klass, meth, keep, root, run_case):
    import sys

    module = sys.modules[klass.__module__]
    orig = klass.__dict__[meth]
    try:
        fn = build(module, klass.__name__, meth, keep, root)
    except Exception:
        return run_case(klass, meth)
    setattr(klass, meth, fn)
    try:
        return run_case(klass, meth)
    finally:
        setattr(klass, meth, orig)


_ = types  # (kept for type references in callers)
