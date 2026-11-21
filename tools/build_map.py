"""Combine tracer output + extras + static import graph into br/map.json.

usage: python tools/build_map.py trace.json extras.json sqlglot_checkout out.json"""

import ast
import json
import os
import sys


def module_files(root):
    out = {}
    for top in ("sqlglot", "tests"):
        for dp, dn, fn in os.walk(os.path.join(root, top)):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if f.endswith(".py"):
                    full = os.path.join(dp, f)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    mod = rel[:-3].replace("/", ".")
                    if mod.endswith(".__init__"):
                        mod = mod[: -len(".__init__")]
                    out[mod] = rel
    return out


def top_level_imports(tree, mod, is_pkg):
    """Imports executed at import time (module level, incl. try/if, excluding TYPE_CHECKING)."""
    res = set()

    def visit(stmts):
        for s in stmts:
            if isinstance(s, ast.Import):
                for a in s.names:
                    res.add(a.name)
            elif isinstance(s, ast.ImportFrom):
                if s.level:
                    parts = mod.split(".")
                    base = parts if is_pkg else parts[:-1]
                    base = base[: len(base) - (s.level - 1)] if s.level > 1 else base
                    b = ".".join(base + ([s.module] if s.module else []))
                else:
                    b = s.module
                res.add(b)
                for a in s.names:
                    res.add(b + "." + a.name)
            elif isinstance(s, ast.If):
                t = ast.unparse(s.test)
                if "TYPE_CHECKING" in t:
                    visit(s.orelse)
                else:
                    visit(s.body)
                    visit(s.orelse)
            elif isinstance(s, ast.Try):
                visit(s.body)
                for h in s.handlers:
                    visit(h.body)
                visit(s.orelse)
                visit(s.finalbody)
            elif isinstance(s, (ast.With,)):
                visit(s.body)

    visit(tree.body)
    return res


def main():
    trace = json.load(open(sys.argv[1]))
    extras = json.load(open(sys.argv[2]))
    root = sys.argv[3]
    tests_json = json.load(open(sys.argv[5]))["tests"] if len(sys.argv) > 5 else None
    mods = module_files(root)
    graph = {}
    for mod, rel in mods.items():
        tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        deps = set()
        for name in top_level_imports(tree, mod, rel.endswith("__init__.py")):
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                cand = ".".join(parts[:i])
                if cand in mods:
                    deps.add(mods[cand])
        deps.discard(rel)
        graph[rel] = deps
    # transitive closure per file
    closure = {}

    def clo(rel, stack=()):
        if rel in closure:
            return closure[rel]
        seen = set()
        todo = [rel]
        while todo:
            x = todo.pop()
            for d in graph.get(x, ()):
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        closure[rel] = seen
        return seen

    importers = {}
    for rel in graph:
        if rel.startswith("tests/"):
            for d in clo(rel):
                importers.setdefault(d, []).append(rel)
    rclosure = {}
    for rel in graph:
        for d in clo(rel):
            rclosure.setdefault(d, []).append(rel)
    mod_importers = {}
    for rel, deps in graph.items():
        if rel.startswith("sqlglot/"):
            for d in deps:
                mod_importers.setdefault(d, []).append(rel)

    ids_by_num = {}
    tj = json.load(open(os.path.join(os.path.dirname(sys.argv[1]), "tests.json")))["tests"]
    for t in tj:
        ids_by_num[str(t["number"])] = t["id"]
    nums = sorted(trace["tests"], key=int)
    ids = [ids_by_num[n] for n in nums]
    pos = {n: i for i, n in enumerate(nums)}
    keys = trace["keys"]
    cover = [[] for _ in keys]
    for n, ks in trace["tests"].items():
        for k in ks:
            cover[k].append(pos[n])
    opens = {}
    for n, fs in trace["opens"].items():
        for f in fs:
            if "__pycache__" in f or f.endswith(".py"):
                continue
            opens.setdefault(f, []).append(pos[n])
    types = {}
    for n, ts in trace["types"].items():
        for t in ts:
            if t.startswith("sqlglot"):
                types.setdefault(t, []).append(pos[n])
    mro = {t: [b for b in bases if b.startswith("sqlglot")] for t, bases in trace["mro"].items() if t.startswith("sqlglot")}
    id_pos = {i: p for p, i in enumerate(ids)}
    pou = {}
    for f, tids in extras["preload_open_users"].items():
        if "__pycache__" in f or f.endswith(".py"):
            continue
        pou[f] = sorted({id_pos[t] for t in tids if t in id_pos})
    preload_opens = sorted(f for f in trace["preload_opens"] if "__pycache__" not in f and not f.endswith(".py"))
    v1 = os.path.join(os.path.dirname(sys.argv[1]), "trace.json")
    cpu_src = json.load(open(v1))["cpu"] if os.path.exists(v1) else trace["cpu"]
    out = {
        "tests": ids,
        "cpu": [cpu_src[n] for n in nums],
        "keys": keys,
        "cover": cover,
        "opens": opens,
        "preload_opens": preload_opens,
        "preload_open_users": pou,
        "preload_keys": trace["preload"],
        "types": types,
        "mro": mro,
        "enums": extras["enums"],
        "importers": importers,
        "mod_importers": mod_importers,
        "rclosure": rclosure,
    }
    lines_path = os.path.join(os.path.dirname(sys.argv[1]), "lines.json")
    if os.path.exists(lines_path):
        L = json.load(open(lines_path))
        flist = L["files"]
        per = {}  # (fi, line) -> list of test pos
        for tid, pairs in L["tests"].items():
            if tid not in id_pos:
                continue
            tp = id_pos[tid]
            for fi, ln in pairs:
                per.setdefault((fi, ln), []).append(tp)
        sets, set_idx, lines = [], {}, {}
        for (fi, ln), tl in per.items():
            key = tuple(sorted(tl))
            si = set_idx.get(key)
            if si is None:
                si = set_idx[key] = len(sets)
                sets.append(list(key))
            lines.setdefault(flist[fi], {})[str(ln)] = si
        out["line_sets"] = sets
        out["lines"] = lines
        out["preload_lines"] = {}
        for fi, ln in L["preload"]:
            out["preload_lines"].setdefault(flist[fi], []).append(ln)
        print("line sets", len(sets), "files", len(lines))
    with open(sys.argv[4], "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("keys", len(keys), "types", len(types), "size", os.path.getsize(sys.argv[4]))


if __name__ == "__main__":
    main()
