"""Synthetic commits that edit one entry of a class-level dict / set literal
(for validating key-level state slicing locally).

usage: python tools/mutate_containers.py sqlglot_checkout outdir N seed"""

import ast
import difflib
import os
import random
import sys


def entries(root):
    out = []
    for top in ("sqlglot/dialects", "sqlglot/parsers", "sqlglot/generators", "sqlglot/tokens.py",
                "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/typing", "sqlglot/dialects/dialect.py"):
        p = os.path.join(root, top)
        files = [p] if p.endswith(".py") else [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs if f.endswith(".py")]
        for f in files:
            src = open(f, encoding="utf-8").read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for st in node.body:
                    if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None:
                        continue
                    v = st.value
                    if isinstance(v, ast.Dict):
                        for k, x in zip(v.keys, v.values):
                            if k is not None and k.lineno == x.end_lineno:
                                out.append((f, "dict", k.lineno, k, x))
                    elif isinstance(v, ast.Set):
                        for e in v.elts:
                            if e.lineno == e.end_lineno:
                                out.append((f, "set", e.lineno, e, None))
    return out


def main():
    root, outdir, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    rng = random.Random(seed)
    os.makedirs(outdir, exist_ok=True)
    ents = entries(root)
    rng.shuffle(ents)
    made = 0
    for f, kind, ln, k, x in ents:
        if made >= n:
            break
        lines = open(f, encoding="utf-8").read().split("\n")
        line = lines[ln - 1]
        # only entries that sit alone on their line
        seg = ast.get_source_segment("\n".join(lines), k)
        if seg is None or line.strip().rstrip(",") != (seg if kind == "set" else line.strip().rstrip(",")):
            continue
        if kind == "set" or rng.random() < 0.5:
            new = None  # delete the entry
        else:
            # swap the value with another entry's value of the same dict
            others = [e for e in ents if e[0] == f and e[1] == "dict" and e[2] != ln and abs(e[2] - ln) < 40]
            if not others:
                continue
            o = rng.choice(others)
            ov = ast.get_source_segment(open(f, encoding="utf-8").read(), o[4])
            cv = ast.get_source_segment(open(f, encoding="utf-8").read(), x)
            if ov is None or cv is None or ov == cv or cv not in line or "\n" in ov:
                continue
            new = line.replace(cv, ov, 1)
        b = lines[: ln - 1] + ([new] if new is not None else []) + lines[ln:]
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        a1 = [l + "\n" for l in lines]
        b1 = [l + "\n" for l in b]
        body = "".join(difflib.unified_diff(a1, b1, f"a/{rel}", f"b/{rel}", n=3))
        if not body:
            continue
        with open(os.path.join(outdir, f"c{seed}x{made:03d}.patch"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body)
        made += 1
    print("made", made)


if __name__ == "__main__":
    main()
