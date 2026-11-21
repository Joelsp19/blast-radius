"""Label synthetic commits with real pytest (runs inside the dev image).

usage: python label.py /m [workers]  -> writes /m/<cid>.json {"failing": [...ids]}"""

import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/br")
import patchlib  # noqa

PLUGIN = r'''
import json, os
FAILED = set()
BAD_FILES = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        BAD_FILES.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["BR_LABEL_OUT"], "w") as f:
        json.dump({"failed": sorted(FAILED), "bad_files": sorted(BAD_FILES), "exit": int(exitstatus)}, f)
'''


def main():
    mdir = sys.argv[1]
    nw = sys.argv[2] if len(sys.argv) > 2 else "4"
    os.makedirs("/tmp/plug", exist_ok=True)
    with open("/tmp/plug/brlabel.py", "w") as f:
        f.write(PLUGIN)
    for p in sorted(glob.glob(os.path.join(mdir, "*.patch"))):
        cid = os.path.basename(p)[:-6]
        out = os.path.join(mdir, cid + ".json")
        lock = os.path.join(mdir, cid + ".lock")
        if os.path.exists(out) or os.path.exists(lock):
            continue
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            continue
        w = "/tmp/w"
        shutil.rmtree(w, ignore_errors=True)
        shutil.copytree("/opt/sqlglot", w, ignore=shutil.ignore_patterns("__pycache__"))
        text = open(p, encoding="utf-8", newline="").read()
        for f in patchlib.parse_patch(text):
            src = open(os.path.join(w, f["old"]), encoding="utf-8", newline="").read()
            new = patchlib.apply_hunks(src, f["hunks"])
            with open(os.path.join(w, f["new"]), "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
        env = dict(os.environ, PYTHONPATH=f"{w}:/tmp/plug", SKIP_INTEGRATION="0",
                   BR_LABEL_OUT="/tmp/label.json")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "brlabel",
                        "-n", nw, "--timeout", "120", "tests"], cwd=w, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = json.load(open("/tmp/label.json"))
        with open(out, "w") as f:
            json.dump(res, f)
        os.remove(lock)
        print(cid, len(res["failed"]), res["bad_files"], flush=True)


if __name__ == "__main__":
    main()
