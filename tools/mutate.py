"""Generate synthetic commits resembling the sample (for local validation only).

usage: python tools/mutate.py sqlglot_checkout outdir N seed"""

import difflib
import os
import random
import re
import sys

OPS = [
    (r"==", "!="), (r"!=", "=="), (r" is not ", " is "), (r" is ", " == "), (r" is ", " is not "),
    (r"<=", "<"), (r"(?<![<-])<(?![<=])", "<="), (r">=", ">"), (r"(?<![>-])>(?![>=])", ">="),
    (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bnot ", ""), (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"), (r"copy=False", "copy=True"),
    (r"copy=True", "copy=False"), (r"\b(\d+)\b", None), (r'"([A-Z_]{3,})"', None),
    (r"\bif ", "if not "), (r"\breturn None\b", "return"), (r"\bNone\b", "0"),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator",
            "# Intentionally not cached", "# TODO: revisit", "# NOTE: order matters here"]
DIRS = ["sqlglot/dialects", "sqlglot/parsers", "sqlglot/generators", "sqlglot/optimizer", "sqlglot/typing",
        "sqlglot/expressions", "sqlglot/executor", "sqlglot", "sqlglot/transforms.py"]


def pyfiles(root):
    out = []
    for dp, dn, fn in os.walk(os.path.join(root, "sqlglot")):
        for f in fn:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
    return sorted(out)


def fixtures(root):
    out = []
    d = os.path.join(root, "tests", "fixtures")
    for dp, dn, fn in os.walk(d):
        for f in fn:
            if f.endswith(".sql") and "tpc" not in dp:
                out.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
    return sorted(out)


def code_mut(rng, lines):
    if len(lines) < 5:
        return None
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(("import", "from", "def ", "class ", "@", '"""')):
            continue
        pat, rep = rng.choice(OPS)
        ms = list(re.finditer(pat, ln))
        if not ms:
            continue
        m = rng.choice(ms)
        if rep is None:
            if m.group(0).isdigit():
                r = str(int(m.group(0)) + rng.choice([1, -1, 1]))
            else:
                r = m.group(0)[:-2] + '_X"'
        else:
            r = rep
        new = ln[: m.start()] + r + ln[m.end():]
        if new == ln:
            continue
        out = list(lines)
        out[i] = new
        return out
    return None


def comment_mut(rng, lines):
    if len(lines) < 5:
        return None
    for _ in range(200):
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        prev = lines[i - 1].rstrip()
        if prev.endswith(("\\", ",", "(", "[", "{")) or '"""' in nxt or "'''" in nxt:
            continue
        ind = nxt[: len(nxt) - len(nxt.lstrip())]
        out = list(lines)
        out.insert(i, ind + rng.choice(COMMENTS) + "\n")
        return out
    return None


def fixture_mut(rng, lines):
    if len(lines) < 2:
        return None
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        if not ln.strip() or ln.startswith(("#", "--")):
            continue
        toks = [m for m in re.finditer(r"\b[a-zA-Z_]\w*\b", ln)]
        if not toks:
            continue
        m = rng.choice(toks)
        kind = rng.randrange(3)
        if kind == 0:
            new = ln[: m.end()] + " 1" + ln[m.end():]
        elif kind == 1:
            new = ln[: m.start()] + m.group(0) + "a" + ln[m.end():]
        else:
            new = ln[: m.start()] + "x " + ln[m.start():]
        out = list(lines)
        out[i] = new
        return out
    return None


def diff(rel, a, b):
    d = difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3)
    body = "".join(d)
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


def main():
    root, outdir, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    rng = random.Random(seed)
    os.makedirs(outdir, exist_ok=True)
    pys = pyfiles(root)
    fx = fixtures(root)
    weights = []
    for p in pys:
        w = 1.0
        if "/dialects/" in p or "/parsers/" in p or "/generators/" in p:
            w = 1.0
        if "/optimizer/" in p or "/typing/" in p:
            w = 2.0
        if p.count("/") == 1:
            w = 3.0
        weights.append(w)

    def read(rel):
        with open(os.path.join(root, rel), encoding="utf-8", newline="") as f:
            return f.read().splitlines(keepends=True)

    made = 0
    while made < n:
        kind = rng.choices(["code", "comment", "fixture", "multi"], [0.5, 0.12, 0.2, 0.18])[0]
        parts = []
        if kind == "code":
            rel = rng.choices(pys, weights)[0]
            new = code_mut(rng, read(rel))
            if new:
                parts.append((rel, new))
        elif kind == "comment":
            rel = rng.choices(pys, weights)[0]
            new = comment_mut(rng, read(rel))
            if new:
                parts.append((rel, new))
        elif kind == "fixture":
            rel = rng.choice(fx)
            new = fixture_mut(rng, read(rel))
            if new:
                parts.append((rel, new))
        else:
            used = set()
            for f, src in ((comment_mut, pys), (code_mut, pys), (fixture_mut, fx)):
                if rng.random() < 0.7:
                    rel = rng.choices(src, weights)[0] if src is pys else rng.choice(src)
                    if rel in used:
                        continue
                    new = f(rng, read(rel))
                    if new:
                        used.add(rel)
                        parts.append((rel, new))
        if not parts:
            continue
        text = "".join(diff(rel, read(rel), new) for rel, new in sorted(parts))
        cid = f"m{seed}x{made:03d}"
        with open(os.path.join(outdir, cid + ".patch"), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        with open(os.path.join(outdir, cid + ".kind"), "w") as f:
            f.write(kind)
        made += 1


if __name__ == "__main__":
    main()
