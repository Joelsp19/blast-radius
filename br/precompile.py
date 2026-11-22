"""Build-time (not charged): convert the gzipped JSON maps to pickles, which load several
times faster at run time. The selector falls back to the JSON files if a pickle is absent."""
import glob
import gzip
import json
import os
import pickle

HERE = os.path.dirname(os.path.abspath(__file__))
for src in [os.path.join(HERE, "map.json.gz")] + glob.glob(os.path.join(HERE, "sub", "*.json.gz")):
    with gzip.open(src, "rb") as f:
        d = json.loads(f.read())
    with open(src[: -len(".json.gz")] + ".pkl", "wb") as f:
        pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
