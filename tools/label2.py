"""Label synthetic commits with the unittest harness on a real patched copy of the tree.

usage: python label2.py /m   (run several containers in parallel; uses lock files)
writes /m/<cid>.json {"failed": [...], "cpu": {id: s}}"""
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/br")
import patchlib  # noqa

RUNNER = r'''
import json, os, signal, sys, time
sys.path.insert(0, "/br")
import harness
harness.SG = sys.argv[1]
harness.setup_env()

class Timeout(BaseException):
    pass

def on_alarm(*a):
    raise Timeout()

signal.signal(signal.SIGALRM, on_alarm)
tests = harness.load_tests_json("/work/tests.json")
want = set(json.load(open("/tmp/ids.json")))
nto = 0
for t in tests:
    if t["id"] not in want:
        continue
    if nto >= 3:
        print(json.dumps([t["id"], 1, 0]), flush=True)
        continue
    t0 = time.process_time()
    signal.alarm(int(max(30, t["seconds"] * 25)))
    try:
        f = harness.run_id(t["id"])
    except Timeout:
        f = True
        nto += 1
    except BaseException:
        f = True
    signal.alarm(0)
    print(json.dumps([t["id"], int(f), round(time.process_time() - t0, 3)]), flush=True)
'''


def main():
    mdir = sys.argv[1]
    with open("/tmp/runner.py", "w") as f:
        f.write(RUNNER)
    tj = json.load(open("/work/tests.json"))["tests"]
    ids = [t["id"] for t in tj]
    heavy = {t["id"] for t in tj if t["seconds"] >= 3.0}
    from cmap import CMap
    from analyze import Analyzer
    an = Analyzer("/opt/sqlglot", CMap("/br/map.json"))
    for p in sorted(glob.glob(os.path.join(mdir, "*.patch"))):
        cid = os.path.basename(p)[:-6]
        out = os.path.join(mdir, cid + ".json")
        lock = os.path.join(mdir, cid + ".lock")
        if os.path.exists(out):
            continue
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        w = "/tmp/w"
        shutil.rmtree(w, ignore_errors=True)
        shutil.copytree("/opt/sqlglot", w)
        text = open(p, encoding="utf-8", newline="").read()
        for f in patchlib.parse_patch(text):
            src = open(os.path.join(w, f["old"]), encoding="utf-8", newline="").read()
            new = patchlib.apply_hunks(src, f["hunks"])
            dst = os.path.join(w, f["new"])
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            shutil.rmtree(os.path.join(os.path.dirname(dst), "__pycache__"), ignore_errors=True)
        plan = an.analyze(cid, text)
        want = [i for i in ids if i not in heavy or i in plan.cands]
        with open("/tmp/ids.json", "w") as fh:
            json.dump(want, fh)
        env = dict(os.environ, PYTHONPATH=w, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "/tmp/runner.py", w], cwd=w, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        failed, cpu = set(), {}
        for ln in proc.stdout.splitlines():
            try:
                tid, f, c = json.loads(ln)
            except Exception:
                continue
            cpu[tid] = c
            if f:
                failed.add(tid)
        # a crash mid-run: everything not reported counts as failed
        for tid in want:
            if tid not in cpu:
                failed.add(tid)
        with open(out, "w") as fh:
            json.dump({"failed": sorted(failed), "cpu": cpu, "ran": want}, fh)
        os.remove(lock)
        print(cid, len(failed), flush=True)


if __name__ == "__main__":
    main()
