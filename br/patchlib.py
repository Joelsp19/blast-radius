"""Unified-diff parsing/applying and bytecode-level semantic diffing."""

import dis
import re
import types

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return list of dicts: {old, new, hunks:[(old_start, old_len, lines)]}.

    `old`/`new` are repo-relative paths or None (for /dev/null)."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "git": ln}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            cur["_seen_hdr"] = False
            i += 1
            continue
        if ln.startswith("--- ") and cur is not None and not cur["hunks"]:
            p = ln[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            i += 1
            continue
        if ln.startswith("+++ ") and cur is not None and not cur["hunks"]:
            p = ln[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            i += 1
            continue
        if ln.startswith("rename from ") and cur is not None:
            cur["old"] = ln[len("rename from "):]
        elif ln.startswith("rename to ") and cur is not None:
            cur["new"] = ln[len("rename to "):]
        elif ln.startswith("new file mode") and cur is not None:
            cur["old"] = None
        elif ln.startswith("deleted file mode") and cur is not None:
            cur["new"] = None
        m = HUNK.match(ln)
        if m and cur is not None:
            ostart = int(m.group(1))
            olen = int(m.group(2)) if m.group(2) is not None else 1
            nlen = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < olen or n < nlen):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == " ":
                    o += 1
                    n += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append((tag, b[1:]))
                i += 1
            # "\ No newline at end of file" markers
            noeol = False
            while i < len(lines) and lines[i].startswith("\\"):
                noeol = True
                i += 1
            cur["hunks"].append((ostart, olen, body, noeol))
            continue
        i += 1
    for f in files:
        f.pop("_seen_hdr", None)
    return files


def apply_hunks(orig, hunks):
    """Apply hunks to original text (str). Returns new text. Raises ValueError on mismatch."""
    src = orig.split("\n")
    # keep track of trailing newline: split gives trailing '' if text ends with \n
    out = []
    pos = 0  # index into src (0-based)
    for ostart, olen, body, _ in hunks:
        start = ostart - 1 if olen > 0 else ostart
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(src[pos:start])
        k = start
        for tag, text in body:
            if tag in " -":
                if k >= len(src) or src[k] != text:
                    raise ValueError(f"context mismatch at line {k + 1}")
                k += 1
            if tag in " +":
                out.append(text)
        pos = k
    out.extend(src[pos:])
    return "\n".join(out)


def changed_old_lines(hunks):
    """Set of old line numbers (1-based) touched; insertions are mapped to the line before
    and after the insertion point."""
    s = set()
    for ostart, olen, body, _ in hunks:
        k = ostart if olen > 0 else ostart + 1
        for tag, _ in body:
            if tag == " ":
                k += 1
            elif tag == "-":
                s.add(k)
                k += 1
            else:
                s.add(k - 1)
                s.add(k)
    return s


# ---------------------------------------------------------------- bytecode diff

def _const_sig(c):
    if isinstance(c, types.CodeType):
        return ("<code>", c.co_qualname)
    if isinstance(c, (tuple, frozenset)):
        return (type(c).__name__, tuple(_const_sig(x) for x in c) if isinstance(c, tuple) else frozenset(_const_sig(x) for x in c))
    # distinguish 1 / 1.0 / True, -0.0 / 0.0
    if isinstance(c, float):
        return ("float", repr(c))
    return (type(c).__name__, c)


def _doc_index(code):
    """Index in co_consts of the docstring, if any (it is never used by the bytecode of a
    function; in module/class bodies it is stored to __doc__)."""
    consts = code.co_consts
    if not consts or not isinstance(consts[0], str):
        return None
    is_body = code.co_name == "<module>" or not (code.co_flags & 0x2)
    instrs = list(dis.get_instructions(code))
    if is_body:
        for a, b in zip(instrs, instrs[1:]):
            if a.opname == "LOAD_CONST" and b.opname in ("STORE_NAME", "STORE_GLOBAL") and b.argval == "__doc__":
                return a.arg
        return None
    for ins in instrs:
        if ins.opname in ("LOAD_CONST", "RETURN_CONST") and ins.arg == 0:
            return None
    return 0


def shallow_sig(code):
    consts = code.co_consts
    if code.co_name != "<module>" and not (code.co_flags & 0x2):
        # class body: 3.13 stores __firstlineno__ as a constant
        consts = tuple("<fl>" if (type(c) is int and c == code.co_firstlineno) else c for c in consts)
    di = _doc_index(code)
    if di is not None:
        # pytest never runs sqlglot's doctests, so docstrings cannot fail a test
        consts = consts[:di] + ("<doc>",) + consts[di + 1:]
    return (
        code.co_code,
        tuple(_const_sig(c) for c in consts),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_flags,
        code.co_name,
        code.co_qualname,
        code.co_exceptiontable,
        code.co_stacksize,
    )


def walk(code, out=None):
    """DFS list of code objects (module code first)."""
    if out is None:
        out = []
    out.append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, out)
    return out


class Diff:
    def __init__(self):
        self.funcs = []  # list of (old_code, new_code) for changed function-level code objects
        self.structural = []  # qualnames of changed module/class bodies or shape mismatch
        self.error = None  # compile error in new source


def _is_body(code):
    # module or class body code objects (not functions/lambdas/comprehensions)
    return code.co_name == "<module>" or not (code.co_flags & 0x2)  # CO_NEWLOCALS


def code_diff(old_src, new_src, filename):
    d = Diff()
    try:
        old = compile(old_src, filename, "exec", dont_inherit=True)
    except SyntaxError as e:
        d.error = e
        return d
    try:
        new = compile(new_src, filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        d.error = e
        return d
    ol, nl = walk(old), walk(new)

    def keyed(lst):
        seen = {}
        res = {}
        for c in lst:
            k = c.co_qualname
            n = seen.get(k, 0)
            seen[k] = n + 1
            res[(k, n)] = c
        return res

    ok, nk = keyed(ol), keyed(nl)
    if set(ok) != set(nk):
        d.structural.append("<shape>")
        # still report differing function objects that exist in both
    for k, oc in ok.items():
        nc = nk.get(k)
        if nc is None:
            continue
        if shallow_sig(oc) != shallow_sig(nc):
            if _is_body(oc):
                d.structural.append(k[0])
            else:
                d.funcs.append((oc, nc))
    return d
