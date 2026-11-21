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

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

DATA = os.environ.get("BR_DATA", "/data")
SG = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
HEAVY = float(os.environ.get("BR_HEAVY", "3.0"))  # seconds; never run tests this slow
PAR = int(os.environ.get("BR_PAR", "2"))
DEBUG = os.environ.get("BR_DEBUG")
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
    newc = statediff.class_keys(plan.slice_mods)
    for k in plan.orig_fp:
        if k not in newc:
            mn, q = k.split(":", 1)
            obj = sys.modules.get(mn)
            for part in q.split("."):
                obj = getattr(obj, part, None) if obj is not None else None
            if isinstance(obj, type):
                newc[k] = obj
    names = statediff.changed_names(plan.orig_fp, statediff.fingerprint(newc))
    names |= statediff.changed_names(plan.orig_mfp, statediff.module_fingerprint(plan.slice_mods))
    keep_kinds = {"line", "fixture"}
    allowed = {t for t in run_ids if plan.why.get(t) in keep_kinds}
    hits = statediff.lines_mentioning(SG, names, cm.line_files())
    textual = set()
    for rel, lines in hits.items():
        for ln in lines:
            textual |= cm.line_tests(rel, ln)
    mentioned = set()
    for rel, lines in hits.items():
        with open(os.path.join(SG, rel), encoding="utf-8") as f:
            text = f.read()
        for n in names:
            if re.search(BOUND + re.escape(n) + BOUND, text):
                mentioned.add(n)
    if names - mentioned:
        # some changed attribute is only reached dynamically: keep all class users
        return run_ids
    if cm.has_attrs:
        # attribute-read coverage: tests that actually loaded one of the changed names
        for n in names:
            allowed |= cm.attr_readers(n)
    else:
        allowed |= textual
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
        if plan.mode == "fresh":
            do_purge(plan, cm)
            if getattr(plan, "orig_fp", None) is not None:
                run_ids = slice_by_state(plan, cm, run_ids, out)
        else:
            do_swaps(plan)
    except BaseException as e:  # noqa
        out.write(f"E {e!r}\n")
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
        self.first_fail_cpu = None

    def cpu_spent(self):
        return self.cpu_used + (proc_cpu(self.pid) if self.pid else 0.0)

    def maybe_stop(self):
        import policy

        cur = [self.ms[t] for t in self.out | self.failed]
        # keep verifying a little after the first failure: how broad the breakage is
        # decides whether heavy tests are at risk
        if self.failed and self.first_fail_cpu is None:
            self.first_fail_cpu = self.cpu_spent()
        ev = len(self.failed) >= 3 or (self.failed and self.cpu_spent() - self.first_fail_cpu > 3.0)
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
        job.cur = None
        job.cur_t = time.time()
    elif line.startswith("K "):
        tid = line[2:]
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
    os.dup2(2, 1)
    from analyze import Analyzer
    from cmap import CMap

    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    id2num = {t["id"]: t["number"] for t in tests}
    ms = {t["id"]: round(t["seconds"] * 1000) for t in tests}
    cm = CMap(os.path.join(APP, "map.json"))
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
            pl.orig_fp = pl.orig_mfp = None
            if pl.mode == "fresh" and pl.slice_mods and not pl.no_slice:
                try:
                    for mn in pl.slice_mods:
                        __import__(mn)
                    pl.orig_fp = statediff.fingerprint(statediff.class_keys(pl.slice_mods))
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
                log(j.plan.cid, j.plan.mode, f"cpu {j.cpu_used:.2f}", "FAILED", len(j.failed), "INCLUDED", len(j.out))

    # expensive tests are never run: include them when the expected miss cost is higher
    failed_by = {j.plan.cid: j.failed for j in jobs}
    for cid, (H, p, sure) in heavies.items():
        sel = results[cid]
        nf = len(failed_by.get(cid, ())) + len(sure)
        for h in sorted(H, key=lambda h: -ms[h]):
            if policy.heavy_include(h, [ms[t] for t in sel], ms, nf, P, why[cid].get(h, "file"), ncl[cid]):
                sel.add(h)

    lines = []
    for cid in cids:
        sel = results.get(cid, set())
        items = [(id2num[t], ms[t]) for t in sel if t in id2num]
        lines.append(json.dumps({"commit": cid, "workers": pack(items)}))
    sys.stdout.flush()
    with os.fdopen(real_out, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    log("done")


if __name__ == "__main__":
    main()
