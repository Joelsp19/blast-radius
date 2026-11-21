"""Offline analysis of sample commits: bytecode diff + coverage candidates vs answers."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "br"))
import patchlib  # noqa

BLAST = os.path.join(os.path.dirname(__file__), "..", "..")
SG = os.path.join(BLAST, "sg")
tests = json.load(open(os.path.join(BLAST, "work/tests.json")))["tests"]
tr = json.load(open(os.path.join(BLAST, "work/trace.json")))
ans = json.load(open(os.path.join(BLAST, "kit/sample/answers.json")))["commits"]
keys = [tuple(k) for k in tr["keys"]]
kidx = {k: i for i, k in enumerate(keys)}
cover = {}  # key idx -> set(tests)
for n, ks in tr["tests"].items():
    for k in ks:
        cover.setdefault(k, set()).add(int(n))
opens = {}
for n, fs in tr["opens"].items():
    for f in fs:
        opens.setdefault(f, set()).add(int(n))
sec = {t["number"]: t["seconds"] for t in tests}

tot_cand = 0
for cid in sorted(ans):
    text = open(os.path.join(BLAST, "kit/sample/commits", cid + ".patch"), encoding="utf-8").read()
    fails = set(ans[cid]["failing"])
    cands = set()
    notes = []
    for f in patchlib.parse_patch(text):
        path = f["old"]
        orig = open(os.path.join(SG, path), encoding="utf-8").read()
        new = patchlib.apply_hunks(orig, f["hunks"])
        if path.endswith(".py"):
            d = patchlib.code_diff(orig, new, path)
            if d.error:
                notes.append(f"{path}: COMPILE ERROR")
                continue
            for oc, nc in d.funcs:
                k = (path, oc.co_qualname, oc.co_firstlineno)
                c = cover.get(kidx.get(k, -1), set())
                cands |= c
                notes.append(f"{path}:{oc.co_qualname} -> {len(c)} tests")
            if d.structural:
                notes.append(f"{path}: STRUCTURAL {d.structural}")
            if not d.funcs and not d.structural:
                notes.append(f"{path}: no bytecode change")
        else:
            c = opens.get(path, set())
            cands |= c
            notes.append(f"{path}: opened by {len(c)}")
    miss = fails - cands
    cs = sum(sec[n] for n in cands)
    tot_cand += cs
    print(cid, ans[cid]["kind"], "fail", len(fails), "cand", len(cands), f"{cs:.1f}s", "MISSING" if miss else "", notes)
print("total cand seconds", tot_cand)
