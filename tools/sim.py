"""Offline policy simulator over labeled synthetic commits + sample commits.

usage: python tools/sim.py [param=value ...]"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "br"))
from analyze import Analyzer  # noqa
from cmap import CMap  # noqa
import policy  # noqa

BL = os.path.join(HERE, "..", "..")
T = json.load(open(os.path.join(BL, "work/tests.json")))["tests"]
ms = {t["id"]: round(t["seconds"] * 1000) for t in T}
cpu1 = json.load(open(os.path.join(BL, "work/trace.json")))["cpu"]
cpu = {T[int(n)]["id"]: max(1, int(v * 1000)) for n, v in cpu1.items()}

P = policy.Params()
for a in sys.argv[1:]:
    k, v = a.split("=")
    setattr(P, k, float(v))
VERBOSE = os.environ.get("V")

cm = CMap(os.path.join(HERE, "..", "br", "map.json"))
an = Analyzer(os.path.join(BL, "sg"), cm)

commits = []
A = json.load(open(os.path.join(BL, "kit/sample/answers.json")))["commits"]
for cid, a in A.items():
    text = open(os.path.join(BL, "kit/sample/commits", cid + ".patch"), encoding="utf-8", newline="").read()
    commits.append((cid, text, {T[i]["id"] for i in a["failing"]}, None))
for f in sorted(glob.glob(os.path.join(BL, "work/m/*.json"))):
    cid = os.path.basename(f)[:-5]
    d = json.load(open(f))
    if sum(1 for t in d["failed"] if d["cpu"].get(t) == 0) > 20:
        continue  # label hit the timeout cap: unreliable
    text = open(f[:-5] + ".patch", encoding="utf-8", newline="").read()
    cpuL = d["cpu"]
    # the labelers ran under a 1.2 GB cap: heavy-test failures are only trusted when
    # re-verified in isolation (tools/verify.py)
    VERIFIED = {"m1x032": {"tests/test_executor.py::TestExecutor::test_execute_tpcds"}}
    truth = {t for t in d["failed"] if cpuL.get(t) is not None
             and (ms[t] < 3000 or t in VERIFIED.get(cid, ()))}
    ran = {t for t in d["ran"] if cpuL.get(t) is not None}
    commits.append((cid, text, truth, ran))

PRIOR = {"fixture": P.p_fixture, "line": P.p_line, "doc": 0.1, "type": P.p_type, "file": 0.02}
import collections
grp = collections.defaultdict(lambda: [0, 0, 0, 0])
tot = {"cpu": 0, "out": 0, "miss": 0, "oracle": 0, "n": 0, "recall_miss": 0}
for cid, text, truth, ran in commits:
    plan = an.analyze(cid, text)
    if ran is not None:
        truth = truth & set(ran)
    oracle = policy.pack_cost([ms[t] for t in truth])
    if plan.mode == "none" or plan.error:
        cands = set()
    else:
        cands = plan.cands
    rmiss = truth - cands
    p = {t: PRIOR[plan.why.get(t, "file")] for t in cands}
    D, H, R = policy.decide_upfront(cands, p, ms, cpu, P)
    jc = cpu
    for h in sorted(H):
        if plan.why.get(h) != "line" or not plan.touched_exact or plan.mode == "fresh":
            continue
        q = cm.sub_queries(h, plan.touched)
        if q is None:
            continue
        H.discard(h)
        if not q:
            continue
        est = int(1000 * (0.5 + 1.3 * sum(cm.subtests[h]["cpu"][i] for i in q)))
        if est > 0.8 * (ms[h] + 2000):
            H.add(h)
            continue
        if jc is cpu:
            jc = dict(cpu)
        jc[h] = est
        R.append(h)
    cpu_c = jc
    F = set()
    spent = 0
    out = set(D)
    remaining = list(R)
    while remaining:
        ev = bool(F) or any(p[t] >= 0.5 for t in D)
        if policy.should_stop([ms[t] for t in out | F], remaining, cpu_c, ms, p, ev):
            out |= set(remaining)
            break
        t = remaining.pop(0)
        spent += cpu_c[t]
        if t in truth:
            F.add(t)
    out |= F
    anyf = bool(F) or any(p[t] >= 0.5 for t in D)
    for h in sorted(H, key=lambda h: -ms[h]):
        ncl = sum(1 for t in cands if plan.why.get(t) == "line" and ms[t] < P.heavy_ms)
        if policy.heavy_include(h, [ms[t] for t in out], ms, anyf, P, plan.why.get(h, "file"), ncl):
            out.add(h)
    miss = bool(truth - out)
    oc = policy.pack_cost([ms[t] for t in out])
    tot["cpu"] += spent
    tot["out"] += oc
    tot["miss"] += miss
    tot["oracle"] += oracle
    tot["n"] += 1
    g = grp["sample" if ran is None else "mut"]
    g[0] += spent; g[1] += oc; g[2] += miss; g[3] += oracle
    tot["recall_miss"] += bool(rmiss)
    if VERBOSE or miss or rmiss:
        print(f"{cid:10s} {plan.mode:5s} cands {len(cands):4d} truth {len(truth):3d} cpu {spent/1000:6.2f} "
              f"out {oc/1000:6.2f} oracle {oracle/1000:6.2f} {'MISS' if miss else ''} "
              f"{'RECALL-MISS ' + str(sorted(rmiss)[:3]) if rmiss else ''} {plan.notes[:3]}")
n = tot["n"]
print(f"n={n} cpu={tot['cpu']/1000:.1f} out={tot['out']/1000:.1f} misses={tot['miss']} "
      f"recall_misses={tot['recall_miss']} oracle={tot['oracle']/1000:.1f} "
      f"total={(tot['cpu'] + tot['out'])/1000 + 300 * tot['miss']:.1f}")
for k, (c, o, mi, orc) in grp.items():
    print(f"  {k}: cpu={c/1000:.1f} out={o/1000:.1f} misses={mi} oracle={orc/1000:.1f}")
