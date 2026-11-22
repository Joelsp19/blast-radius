"""Blast Radius selector.

1. Static phase (cheap, no sqlglot import): apply each patch in memory, diff bytecode
   against the pinned commit, and use the coverage map to find candidate tests.
2. Dynamic phase: a warm "zygote" process holds sqlglot + the needed test modules.
   For each commit a child is forked that either hot-swaps the changed function code
   objects in place, or purges and re-imports just the modules that depend on the
   change from an in-memory overlay. The child runs the candidate tests and reports
   which fail. Expensive tests are never run: they are included if they are candidates.
3. Output: only failing (+ included expensive) tests, packed over <= 8 workers.
"""

import gc
import json
import os
import re
import select
import signal
import sys
import time
import types

import flags

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

DATA = os.environ.get("BR_DATA", "/data")
SG = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
HEAVY = float(os.environ.get("BR_HEAVY", "3.0"))  # seconds; never run tests this slow
PAR = int(os.environ.get("BR_PAR", "2"))
DEBUG = os.environ.get("BR_DEBUG")
HNF = int(os.environ.get("BR_HNF", "10"))
HDT = int(os.environ.get("BR_HDT", "5000"))
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ packing

def pack(items):
    """items: list of (num, ms). Choose worker count/assignment minimizing max load + 2s*w."""
    if not items:
        return []
    items = sorted(items, key=lambda x: -x[1])
    best = None
    for w in range(1, min(8, len(items)) + 1):
        loads = [0] * w
        bins = [[] for _ in range(w)]
        for n, ms in items:
            i = loads.index(min(loads))
            loads[i] += ms
            bins[i].append(n)
        cost = max(loads) + 2000 * w
        if best is None or cost < best[0]:
            best = (cost, bins)
    return [b for b in best[1] if b]


# ------------------------------------------------------------------ child side

FINDEX = {}


def build_findex():
    pre = SG.rstrip("/") + "/"
    for o in gc.get_objects():
        if type(o) is types.FunctionType:
            c = o.__code__
            if c.co_filename.startswith(pre):
                FINDEX.setdefault((c.co_filename, c.co_qualname, c.co_firstlineno), []).append(o)


def do_swaps(plan):
    for path, swaps in plan.swaps:
        for oc, nc, own, parent in swaps:
            for f in FINDEX.get((oc.co_filename, oc.co_qualname, oc.co_firstlineno), ()):
                f.__code__ = nc


def do_purge(plan, cm):
    """Forget every sqlglot / tests module so the patched tree is imported from scratch.

    (A partial purge of reverse dependencies is not safe: sqlglot resolves dialects through
    dynamic imports that a static import graph does not see.)"""
    import harness

    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]
    harness._mods.clear()
    harness._class_state.clear()
    FINDEX.clear()


NL = chr(10)
BOUND = chr(92) + "b"


def slice_by_state(plan, cm, run_ids, out):
    """Keep only tests that can observe a changed class/module attribute (see statediff)."""
    import importlib

    import statediff

    mods = set(plan.slice_mods) | {k.split(":", 1)[0] for k in plan.orig_fp}
    for mn in sorted(mods):
        try:
            importlib.import_module(mn)
        except BaseException:  # noqa
            return run_ids  # cannot compare: run everything
    if DEBUG:
        log(plan.cid, "child imported", round(time.process_time(), 2))
    newc = statediff.class_keys(plan.slice_mods)
    for k in plan.orig_fp:
        if k not in newc:
            mn, q = k.split(":", 1)
            obj = sys.modules.get(mn)
            for part in q.split("."):
                obj = getattr(obj, part, None) if obj is not None else None
            if isinstance(obj, type):
                newc[k] = obj
    newfp = statediff.fingerprint(newc)
    cnames = statediff.changed_names(plan.orig_fp, newfp)
    names = set(cnames)
    # key-level: a changed dict / set attribute is only observed by tests that read it as a
    # whole or looked up one of the changed keys
    keytests = {}
    if os.environ.get("BR_NOKEYS") or flags.NO_NEWSLICE:
        plan.orig_kfp = None
    newk = statediff.container_fingerprint(newc) if plan.orig_kfp is not None else {}
    for ck in set(plan.orig_fp) | set(newfp):
        a, b = plan.orig_fp.get(ck), newfp.get(ck)
        if a is None or b is None:
            continue
        for n in set(a) | set(b):
            if a.get(n) == b.get(n):
                continue
            lab = ck + "." + n
            r = None
            if plan.orig_kfp is not None and lab in plan.orig_kfp and lab in newk:
                r = cm.key_readers(lab, statediff.changed_keys(plan.orig_kfp[lab], newk[lab]))
            if r is None:
                keytests[n] = False
            elif keytests.get(n, True) is not False:
                keytests.setdefault(n, set()).update(r)
    newm = statediff.module_fingerprint(plan.slice_mods)
    # a module global is only visible through that module: its own code and its importers
    mod_scope = {}
    for mn in set(plan.orig_mfp) | set(newm):
        a, b = plan.orig_mfp.get(mn, {}), newm.get(mn, {})
        rel = None
        for cand in (mn.replace(".", "/") + ".py", mn.replace(".", "/") + "/__init__.py"):
            if os.path.exists(os.path.join(SG, cand)):
                rel = cand
                break
        for n in set(a) | set(b):
            if a.get(n) != b.get(n):
                if rel is None:
                    mod_scope[n] = None
                elif mod_scope.get(n, 0) is not None:
                    sc = mod_scope.setdefault(n, set())
                    sc.add(rel)
                    sc.update(cm.mod_importers.get(rel, ()))
                    sc.update(cm.importers.get(rel, ()))
    for n, sc in mod_scope.items():
        if n in names:
            continue  # also a class attribute: visible anywhere
        names.add(n)
    scoped = {n: sc for n, sc in mod_scope.items() if sc is not None and n not in cnames}
    if flags.NO_NEWSLICE:
        scoped = {}
    if DEBUG:
        log(plan.cid, "child fingerprinted", round(time.process_time(), 2))
    keep_kinds = {"line", "fixture"}
    allowed = {t for t in run_ids if plan.why.get(t) in keep_kinds}
    hits = statediff.lines_mentioning(SG, names, cm.line_files())
    mentioned = set()
    textual = {n: set() for n in names}
    for rel, lines in hits.items():
        with open(os.path.join(SG, rel), encoding="utf-8") as f:
            src_lines = f.read().split(NL)
        for ln in lines:
            txt = src_lines[ln - 1] if ln - 1 < len(src_lines) else ""
            tests = cm.line_tests(rel, ln)
            for n in names:
                if n in scoped and rel not in scoped[n]:
                    continue
                if re.search(BOUND + re.escape(n) + BOUND, txt):
                    mentioned.add(n)
                    textual[n] |= tests
    for n in scoped:
        if n not in mentioned:
            pat = re.compile(BOUND + re.escape(n) + BOUND)
            for rel in scoped[n]:
                try:
                    with open(os.path.join(SG, rel), encoding="utf-8") as f:
                        if pat.search(f.read()):
                            mentioned.add(n)  # only on lines no test executes
                            break
                except OSError:
                    pass
    if names - mentioned:
        # some changed attribute is only reached dynamically: keep all class users
        return run_ids
    for n in names:
        # both are over-approximations of the tests that read `n`; so is their intersection
        obs = (textual[n] & cm.attr_readers(n)) if cm.has_attrs else textual[n]
        kt = keytests.get(n)
        if kt:
            obs = obs & kt
        elif kt is not None and kt is not False:
            obs = set()  # no test reads the changed keys
        allowed |= obs
    keep = [t for t in run_ids if t in allowed]
    out.write("N " + ",".join(sorted(names))[:2000] + NL)
    for t in run_ids:
        if t not in allowed:
            out.write("K " + t + NL)
    return keep


def child_main(plan, run_ids, wfd, cm):
    import harness
    import overlay

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if not DEBUG:
        os.dup2(devnull, 2)
    out = os.fdopen(wfd, "w", buffering=1)
    try:
        tmp = f"/tmp/br/{plan.cid}"
        overlay.install(SG, plan.files, tmp)
        harness.EAGER[0] = plan.mode == "fresh" and bool(plan.no_slice)
        if plan.mode == "fresh":
            do_purge(plan, cm)
            if getattr(plan, "orig_fp", None) is not None:
                hv = [h for h in getattr(plan, "heavy_ids", ()) if h not in run_ids]
                keep = slice_by_state(plan, cm, run_ids + hv, out)
                run_ids = [t for t in keep if t not in hv]
        else:
            do_swaps(plan)
    except BaseException as e:  # noqa
        out.write(f"E {e!r}\n")
    if DEBUG:
        log(plan.cid, "child sliced", round(time.process_time(), 2), len(run_ids))
    harness.zygote_mode(False)
    # class fixtures first (not charged to any test's timeout)
    out.write("S __setup__\n")
    for tid in run_ids:
        try:
            klass, _ = harness.get_class(tid)
            if klass is not None:
                harness.class_setup(klass)
        except BaseException:  # noqa
            pass
    out.write("R 0 __setup__\n")
    if DEBUG:
        log(plan.cid, "child setup done", round(time.process_time(), 2))
    for tid in run_ids:
        out.write(f"S {tid}\n")
        try:
            sub = getattr(plan, "subsets", {}).get(tid)
            failed = harness.run_id(tid, *sub) if sub else harness.run_id(tid)
        except BaseException:  # noqa
            failed = True
        out.write(f"R {int(failed)} {tid}\n")
    out.flush()
    os._exit(0)


# ------------------------------------------------------------------ parent side

class Job:
    def __init__(self, plan, run_ids, timeouts, out=(), ms=None, cpu=None, p=None):
        self.plan = plan
        self.todo = list(run_ids)
        self.timeouts = timeouts
        self.failed = set()
        self.out = set(out)  # included without running
        self.ms, self.cpu, self.p = ms, cpu, p
        self.cpu_used = 0.0
        self.est_done = 0
        self.heavy = ()  # heavy candidates decided after the run
        self.unobs = set()  # tests shown unable to observe the change (state slicing)
        self.first_fail_cpu = None

    def cpu_spent(self):
        return self.cpu_used + (proc_cpu(self.pid) if self.pid else 0.0)

    def maybe_stop(self):
        import policy

        cur = [self.ms[t] for t in self.out | self.failed]
        # keep verifying a little after the first failure: how broad the breakage is
        # decides whether heavy tests are at risk
        # (estimated, not measured, cpu: decisions must not depend on machine noise)
        if self.failed and self.first_fail_cpu is None:
            self.first_fail_cpu = self.est_done
        # with heavy tests at stake, how broad the breakage is decides them: look further
        nf, dt = (HNF, HDT) if self.heavy else (3, 3000)
        ev = len(self.failed) >= nf or (self.failed and self.est_done - self.first_fail_cpu > dt)
        if self.todo and policy.should_stop(cur, self.todo, self.cpu, self.ms, self.p, ev):
            self.out |= set(self.todo)
            self.todo = []
            return True
        return False
        self.pid = None
        self.rfd = None
        self.buf = b""
        self.cur = None
        self.cur_t = None


def spawn(job, cm):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            child_main(job.plan, list(job.todo), w, cm)
        finally:
            os._exit(1)
    os.close(w)
    job.pid, job.rfd, job.buf, job.cur, job.cur_t = pid, r, b"", None, time.time()


def proc_cpu(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def handle_line(job, line):
    if line.startswith("S "):
        job.cur = line[2:]
        job.cur_t = time.time()
        job.cur_cpu = proc_cpu(job.pid)
    elif line.startswith("R "):
        _, f, tid = line.split(" ", 2)
        if tid == "__setup__":
            job.cur = None
            return
        if f == "1":
            job.failed.add(tid)
        if tid in job.todo:
            job.todo.remove(tid)
        job.est_done += job.cpu.get(tid, 0) if job.cpu else 0
        job.cur = None
        job.cur_t = time.time()
    elif line.startswith("K "):
        tid = line[2:]
        job.unobs.add(tid)
        if tid in job.todo:
            job.todo.remove(tid)
    elif line.startswith("N "):
        if DEBUG:
            log(job.plan.cid, "changed state:", line[2:200])
    elif line.startswith("E "):
        log(job.plan.cid, "child setup error", line[2:])


def finish_child(job, killed):
    try:
        os.close(job.rfd)
    except OSError:
        pass
    try:
        _, _, ru = os.wait4(job.pid, 0)
        job.cpu_used += ru.ru_utime + ru.ru_stime
    except ChildProcessError:
        pass
    job.pid = None
    if job.cur == "__setup__":
        # fixtures hung or crashed: every remaining test is presumed failing
        job.failed |= set(job.todo)
        job.todo = []
        job.cur = None
    if job.cur is not None and killed:
        # too slow: include it (never a miss) but it is no evidence the commit fails
        job.out.add(job.cur)
        if job.cur in job.todo:
            job.todo.remove(job.cur)
        job.cur = None
    if job.cur is not None:
        # the running test crashed the child: it fails
        job.failed.add(job.cur)
        if job.cur in job.todo:
            job.todo.remove(job.cur)
        job.cur = None
    elif job.todo and not killed:
        # child died between tests without reporting: count the next test as failing
        t = job.todo.pop(0)
        job.failed.add(t)


def run_jobs(jobs, cm, deadline):
    pending = list(jobs)
    active = []
    while pending or active:
        if time.time() > deadline:
            # out of wall time: stop verifying, include whatever is left (never a miss)
            log("deadline reached: including remaining candidates")
            for j in active:
                try:
                    os.kill(j.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                j.cur = None
                finish_child(j, killed=True)
            for j in active + pending:
                j.out |= set(j.todo)
                j.todo = []
            return
        while pending and len(active) < PAR:
            j = pending.pop(0)
            if not j.todo:
                continue
            spawn(j, cm)
            active.append(j)
        if not active:
            break
        rl = [j.rfd for j in active]
        ready, _, _ = select.select(rl, [], [], 0.2)
        now = time.time()
        for j in list(active):
            if j.rfd in ready:
                data = os.read(j.rfd, 65536)
                if not data:
                    finish_child(j, killed=False)
                    active.remove(j)
                    if j.todo:
                        pending.insert(0, j)
                    continue
                j.buf += data
                *lines, j.buf = j.buf.split(b"\n")
                for ln in lines:
                    handle_line(j, ln.decode("utf-8", "replace"))
                if any(ln.startswith(b"R ") for ln in lines) and j.maybe_stop():
                    try:
                        os.kill(j.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    j.cur = None  # the interrupted test is among those included
                    finish_child(j, killed=True)
                    active.remove(j)
                    continue
            if j.pid is not None and j.cur is not None:
                lim = j.timeouts.get(j.cur, 60.0)
                used = proc_cpu(j.pid) - j.cur_cpu
                if used > lim or now - j.cur_t > 6 * lim + 30:
                    log(j.plan.cid, "timeout", j.cur)
                    try:
                        os.kill(j.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finish_child(j, killed=True)
                    active.remove(j)
                    if j.todo:
                        pending.insert(0, j)


def main():
    # keep stdout clean: anything printed while we work goes to stderr
    real_out = os.dup(1)
    REAL_OUT[0] = real_out
    os.dup2(2, 1)
    from analyze import Analyzer
    from cmap import CMap

    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    id2num = {t["id"]: t["number"] for t in tests}
    ms = {t["id"]: round(t["seconds"] * 1000) for t in tests}
    cm = CMap(os.path.join(APP, "map.json.gz"))
    unknown = set(id2num) - set(cm.ids)
    an = Analyzer(SG, cm)
    cdir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))

    plans = []
    for cid in cids:
        with open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        try:
            plan = an.analyze(cid, text)
        except Exception as e:  # noqa
            from analyze import Plan

            plan = Plan(cid)
            plan.error = f"analyze: {e!r}"
        plans.append(plan)
    log("analyzed", len(plans))

    import policy

    P = policy.Params()
    PRIOR = {"fixture": P.p_fixture, "line": P.p_line, "doc": 0.1, "type": P.p_type, "file": 0.02}
    cpu = {t: max(1, int(cm.cpu.get(t, ms[t] / 1000) * 1000)) for t in id2num}
    results = {}
    heavies = {}
    why, ncl = {}, {}
    jobs = []
    for plan in plans:
        if plan.error:
            log(plan.cid, "ERROR", plan.error, "-> include everything")
            results[plan.cid] = set(id2num)
            continue
        if plan.mode == "none":
            results[plan.cid] = set()
            continue
        cands = {c for c in plan.cands if c in id2num} | unknown
        p = {c: PRIOR[plan.why.get(c, "file")] for c in cands}
        D, H, R = policy.decide_upfront(cands, p, ms, cpu, P)
        # sub-test slicing: a heavy test made of independent queries is run on just the
        # queries that execute the changed lines (or dropped if none does)
        plan.subsets = {}
        jcpu = cpu
        D0, H0, R0 = set(D), set(H), list(R)
        try:
            for h in sorted(H):
                if plan.why.get(h) != "line" or not plan.touched_exact or plan.mode == "fresh":
                    continue
                q = cm.sub_queries(h, plan.touched)
                if q is None:
                    continue
                H.discard(h)
                if not q:
                    continue
                st = cm.subtests[h]
                est = int(1000 * (0.5 + 1.3 * sum(st["cpu"][i] for i in q)))
                if est > 0.8 * (ms[h] + 2000):
                    H.add(h)  # slicing does not pay: decide by policy as a heavy test
                    continue
                if jcpu is cpu:
                    jcpu = dict(cpu)
                jcpu[h] = est
                plan.subsets[h] = (q, st["attr"])
                R.append(h)
            # fixture-item slicing: loop-over-fixture tests run on just the items that execute a
            # changed line (plus the code outside the loop)
            if plan.mode != "fresh" and plan.touched_exact and not flags.NO_NEWSLICE:
                for t in sorted(set(D) | H | set(R)):
                    if plan.why.get(t) != "line" or t in plan.subsets:
                        continue
                    fs = cm.fixture_slice(t, plan.touched)
                    if fs is None:
                        continue
                    q, base_hit = fs
                    if not q and not base_hit:
                        continue  # coverage not reproduced in isolation: leave as is
                    st = cm.subtests[t]
                    part = st["base_cpu"] + sum(st["cpu"][i] for i in q)
                    if st["attr"] == "__stmts__":
                        # traced per-statement cpu carries tracing overhead: rescale to the test
                        tot = st["base_cpu"] + sum(st["cpu"])
                        est = int(1000 * (0.1 + 1.2 * cm.cpu.get(t, tot) * part / max(tot, 1e-6)))
                    else:
                        est = int(1000 * (0.15 + 1.3 * part))
                    full = jcpu[t]
                    if t in H:
                        if est > 0.8 * (ms[t] + 2000):
                            continue
                    elif est >= 0.8 * full:
                        continue
                    elif t in D and est > (1 - p[t]) * (ms[t] + 2000):
                        continue
                    if jcpu is cpu:
                        jcpu = dict(cpu)
                    jcpu[t] = est
                    plan.subsets[t] = (q, st["attr"])
                    H.discard(t)
                    D.discard(t)
                    if t not in R:
                        R.append(t)
                R.sort(key=lambda t: (-p[t] / max(jcpu[t], 1), jcpu[t]))
        except Exception as e:  # noqa
            log(plan.cid, "slicing failed", repr(e))
            D, H, R = D0, H0, R0
            plan.subsets = {}
            jcpu = cpu
        results[plan.cid] = set(D)
        heavies[plan.cid] = (H, p, {t for t in D if p[t] >= 0.5})
        why[plan.cid] = plan.why
        ncl[plan.cid] = sum(1 for t in cands if plan.why.get(t) == "line" and ms[t] < P.heavy_ms)
        if DEBUG:
            log(plan.cid, plan.mode, "cands", len(cands), "inc", len(D), "heavy", len(H), "run", len(R), plan.notes)
        if R:
            # a test that runs much slower than expected is killed and included: never a miss
            k, base = (3.0, 3.0) if plan.mode == "fresh" else (2.0, 1.5)
            timeouts = {c: k * jcpu[c] / 1000 + base for c in R}
            j = Job(plan, R, timeouts, D, ms, jcpu, p)
            j.heavy = tuple(sorted(H))
            plan.heavy_ids = j.heavy
            if not j.maybe_stop():
                jobs.append(j)
            else:
                results[plan.cid] |= j.out

    if jobs:
        # warm the zygote with what the jobs need
        import harness

        harness.setup_env()
        harness.zygote_mode(True)
        need = sorted({t for j in jobs for t in j.todo})
        harness.preload(need, dialects=True, setups=True)
        build_findex()
        log("zygote ready", len(need), "tests preloaded, cpu", round(time.process_time(), 2))
        # swaps that cannot reach any live function -> purge mode
        for j in jobs:
            if j.plan.mode == "swap":
                for path, swaps in j.plan.swaps:
                    found = {id(oc) for oc, nc, own, parent in swaps
                             if FINDEX.get((oc.co_filename, oc.co_qualname, oc.co_firstlineno))}
                    byid = {id(oc): (oc, parent) for oc, nc, own, parent in swaps}
                    for oc, nc, own, parent in swaps:
                        if not own:
                            continue
                        x, ok = oc, False
                        while x is not None:
                            if id(x) in found:
                                ok = True
                                break
                            x = byid.get(id(x), (None, None))[1]
                        if not ok:
                            log(j.plan.cid, "unswappable", oc.co_qualname, "-> fresh")
                            j.plan.mode = "fresh"
        import statediff

        for j in jobs:
            pl = j.plan
            pl.orig_fp = pl.orig_mfp = pl.orig_kfp = None
            if pl.mode == "fresh" and pl.slice_mods and not pl.no_slice:
                try:
                    for mn in pl.slice_mods:
                        __import__(mn)
                    ck = statediff.class_keys(pl.slice_mods)
                    pl.orig_fp = statediff.fingerprint(ck)
                    pl.orig_kfp = statediff.container_fingerprint(ck) if cm.key_labels else None
                    pl.orig_mfp = statediff.module_fingerprint(pl.slice_mods)
                except BaseException as e:  # noqa
                    log(pl.cid, "fingerprint failed", e)
                    pl.orig_fp = None
        gc.freeze()
        # practice: 3 min for the batch, final: 4.5 min for ~1.5x commits (~6 s per commit)
        budget = float(os.environ.get("BR_BUDGET", min(230.0, max(100.0, 4.8 * len(cids)))))
        run_jobs(jobs, cm, T0 + budget)
        for j in jobs:
            results[j.plan.cid] |= j.failed | j.out
            if DEBUG:
                log("FAILEDSET", j.plan.cid, json.dumps(sorted(j.failed)), json.dumps(sorted(j.out)))
                log(j.plan.cid, j.plan.mode, f"cpu {j.cpu_used:.2f} est {j.est_done / 1000:.2f}", "FAILED", len(j.failed), "INCLUDED", len(j.out))

    # expensive tests are never run: include them when the expected miss cost is higher
    failed_by = {j.plan.cid: j.failed for j in jobs}
    unobs = {j.plan.cid: j.unobs for j in jobs}
    for cid, (H, p, sure) in heavies.items():
        sel = results[cid]
        nf = len(failed_by.get(cid, ())) + len(sure)
        for h in sorted(H, key=lambda h: -ms[h]):
            if h in unobs.get(cid, ()) and not flags.NO_UNOBS:
                continue
            if policy.heavy_include(h, [ms[t] for t in sel], ms, nf, P, why[cid].get(h, "file"), ncl[cid]):
                sel.add(h)

    lines = []
    for i, cid in enumerate(cids):
        sel = results.get(cid, set())
        if flags.DIAG_BIT is not None and ((i + 1) >> flags.DIAG_BIT) & 1:
            sel = set(id2num)  # diagnostic: everything for this commit (cannot miss)
        items = [(id2num[t], ms[t]) for t in sel if t in id2num]
        lines.append(json.dumps({"commit": cid, "workers": pack(items)}))
    sys.stdout.flush()
    OUT_DONE[0] = True
    with os.fdopen(real_out, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    log("done")


def fallback(e):
    """Last resort if anything above failed before writing: a valid, conservative answer
    (every test for every commit)."""
    import traceback

    traceback.print_exc()
    if OUT_DONE[0]:
        return
    try:
        with open(os.path.join(DATA, "tests.json")) as f:
            tests = json.load(f)["tests"]
        items = [(t["number"], round(t["seconds"] * 1000)) for t in tests]
        workers = pack(items)
        cids = sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
        fd = REAL_OUT[0] if REAL_OUT[0] is not None else 1
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps({"commit": c, "workers": workers}) + NL for c in cids))
    except BaseException:  # noqa
        traceback.print_exc()


REAL_OUT = [None]
OUT_DONE = [False]

if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa
        fallback(e)
