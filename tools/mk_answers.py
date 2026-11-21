"""Collect labeled mutants into a scorer-compatible directory: outdir/commits/*.patch + answers.json"""
import glob, json, os, shutil, sys
src, out = sys.argv[1], sys.argv[2]
T = json.load(open(os.path.join(os.path.dirname(src.rstrip("/")), "tests.json")))["tests"]
num = {t["id"]: t["number"] for t in T}
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
ans = {}
for f in sorted(glob.glob(os.path.join(src, "*.json"))):
    cid = os.path.basename(f)[:-5]
    d = json.load(open(f))
    shutil.copy(f[:-5] + ".patch", os.path.join(out, "commits", cid + ".patch"))
    ans[cid] = {"failing": sorted(num[t] for t in d["failed"]), "kind": open(f[:-5] + ".kind").read() if os.path.exists(f[:-5] + ".kind") else "?"}
json.dump({"commits": ans}, open(os.path.join(out, "answers.json"), "w"))
shutil.copy(os.path.join(os.path.dirname(src.rstrip("/")), "tests.json"), os.path.join(out, "tests.json"))
print(len(ans))
