"""Build-time (not charged): convert the gzipped JSON maps to pickles, which load several
times faster at run time. The selector falls back to the JSON files if a pickle is absent."""
import glob
import gzip
import json
import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for src in ([] if len(sys.argv) > 1 else [os.path.join(HERE, "map.json.gz")]) + ([] if len(sys.argv) > 1 else glob.glob(os.path.join(HERE, "sub", "*.json.gz"))):
    with gzip.open(src, "rb") as f:
        d = json.loads(f.read())
    with open(src[: -len(".json.gz")] + ".pkl", "wb") as f:
        pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)


def import_state():
    """Fingerprint of sqlglot's import-time state after importing every module in a fixed
    order (the selector's child compares a patched fresh import against it)."""
    import importlib
    import sys

    sys.path.insert(0, HERE)
    import statediff

    root = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
    mods = []
    for dp, dns, fs in os.walk(os.path.join(root, "sqlglot")):
        dns.sort()
        for f in sorted(fs):
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, f), root)[:-3].replace(os.sep, ".")
                mods.append(rel[: -len(".__init__")] if rel.endswith(".__init__") else rel)
    ok = []
    for mn in sorted(set(mods)):
        try:
            importlib.import_module(mn)
            ok.append(mn)
        except BaseException:  # noqa
            pass
    return ok, statediff.fingerprint(statediff.class_keys(ok)), statediff.module_fingerprint(ok)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "state1":
    import unittest  # noqa: the selector's child has it imported (sqlglot checks for it)

    with open(sys.argv[2], "wb") as f:
        pickle.dump(import_state(), f, protocol=pickle.HIGHEST_PROTOCOL)
elif __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "state":
    import subprocess
    import tempfile

    sys.path.insert(0, HERE)
    import statediff

    runs = []
    for i in range(2):
        out = os.path.join(tempfile.gettempdir(), f"br_state{i}.pkl")
        subprocess.run([sys.executable, os.path.abspath(__file__), "state1", out], check=True)
        with open(out, "rb") as f:
            runs.append(pickle.load(f))
    (mods, fp, mfp), (mods2, fp2, mfp2) = runs
    # names that differ between two identical fresh imports (set ordering of classes, ...)
    noisy = statediff.changed_names(fp, fp2) | statediff.changed_names(mfp, mfp2)
    if mods != mods2:
        noisy = None  # not reproducible: the selector then never trusts the check
    print("import state: modules", len(mods), "noisy", sorted(noisy or ()), file=sys.stderr)
    with open(os.path.join(HERE, "import_fp.pkl"), "wb") as f:
        pickle.dump((mods, fp, mfp, noisy), f, protocol=pickle.HIGHEST_PROTOCOL)
