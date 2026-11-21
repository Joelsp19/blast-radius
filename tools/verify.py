"""Run specific tests against a patched copy of the tree (fresh process, plenty of memory).
usage: python verify.py patch test_id [test_id ...]"""
import os, shutil, subprocess, sys, json
sys.path.insert(0, "/br")
import patchlib
w = "/tmp/w"
shutil.rmtree(w, ignore_errors=True)
shutil.copytree("/opt/sqlglot", w)
text = open(sys.argv[1], encoding="utf-8", newline="").read()
for f in patchlib.parse_patch(text):
    src = open(os.path.join(w, f["old"]), encoding="utf-8", newline="").read()
    dst = os.path.join(w, f["new"])
    open(dst, "w", encoding="utf-8", newline="").write(patchlib.apply_hunks(src, f["hunks"]))
    shutil.rmtree(os.path.join(os.path.dirname(dst), "__pycache__"), ignore_errors=True)
code = r'''
import sys, time, json, unittest
sys.path.insert(0, "/br")
import harness
harness.SG = sys.argv[1]; harness.setup_env()
for tid in sys.argv[2:]:
    k, m = harness.get_class(tid)
    harness.class_setup(k)
    r = unittest.TestResult(); t0 = time.process_time(); k(m).run(r)
    msg = (r.failures + r.errors)[0][1][-600:] if (r.failures or r.errors) else ""
    print(json.dumps([tid.split("::")[-1], not r.wasSuccessful(), round(time.process_time() - t0, 1), msg]))
'''
env = dict(os.environ, PYTHONPATH=w, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
subprocess.run([sys.executable, "-c", code, w] + sys.argv[2:], cwd=w, env=env, stderr=subprocess.DEVNULL)
