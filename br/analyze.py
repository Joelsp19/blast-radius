"""Static analysis of one commit: what changed, which tests can see it, how to verify.

Produces a Plan:
  mode:  "none"  -> nothing observable changed, run nothing
         "swap"  -> only function bodies changed; hot-swap them into the warm zygote
         "fresh" -> anything else; import the patched tree from scratch in a child
  cands: test ids that may fail
  files: relpath -> new text (None = deleted) for every changed file
  swaps: list of (relpath, [(old_code, new_code, own_change, parent_old_code), ...])
"""

import ast
import os

import patchlib

PY_ROOTS = ("sqlglot/", "tests/")


def _pairs(text):
    """The (sql, expected) statement pairs tests/helpers.load_sql_fixture_pairs yields."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("--")]
    st = chr(10).join(lines).split(";")
    return [(st[i], st[i + 1]) for i in range(0, len(st), 2) if i + 1 < len(st)]


def changed_pairs(old, new):
    """Indices of the changed statement pairs, or None if pairs were added or removed."""
    a, b = _pairs(old), _pairs(new)
    if len(a) != len(b):
        return None
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


def class_fixture_attrs(root):
    """Fixture map read from the pinned test sources: query-pair fixture files loaded into a
    class attribute by setUpClass -> (attr, [test ids reading self.<attr>])."""
    out = {}
    for rel in ("tests/test_executor.py",):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            continue
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            meths = {f.name: f for f in cls.body if isinstance(f, ast.FunctionDef)}
            setup = meths.get("setUpClass")
            if setup is None:
                continue
            for n in ast.walk(setup):
                if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Attribute)):
                    continue
                call = n.value
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "list" and call.args:
                    call = call.args[0]
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "load_sql_fixture_pairs"
                        and call.args and isinstance(call.args[0], ast.Constant)):
                    continue
                attr = n.targets[0].attr
                users = []
                for name, f in meths.items():
                    if not name.startswith("test"):
                        continue
                    if any(isinstance(x, ast.Attribute) and x.attr == attr for x in ast.walk(f)):
                        users.append(f"{rel}::{cls.name}::{name}")
                out["tests/fixtures/" + call.args[0].value] = (attr, users)
    return out


class Plan:
    def __init__(self, cid):
        self.cid = cid
        self.mode = "none"
        self.cands = set()
        self.all_tests = False
        self.files = {}
        self.swaps = []
        self.notes = []
        self.error = None
        self.why = {}  # test id -> kind with the highest prior
        self.touched = set()  # (path, line) of statements touched by function-body changes
        self.touched_exact = True  # False when some change fell back to function coverage
        self.slice_mods = set()  # modules whose class/module bodies changed (state-diff slicing)
        self.no_slice = False  # some change other than body changes forces fresh mode

    RANK = {"fixture": 5, "line": 4, "doc": 3, "type": 2, "file": 1}

    def add(self, kind, tests):
        for t in tests:
            self.cands.add(t)
            old = self.why.get(t)
            if old is None or self.RANK[kind] > self.RANK[old]:
                self.why[t] = kind

    def escalate(self, mode):
        order = {"none": 0, "swap": 1, "fresh": 2}
        if order[mode] > order[self.mode]:
            self.mode = mode


def deep_changed(old, new):
    """Return list of (old_code, new_code) for every code object that changed itself or in a
    descendant, keyed by qualname order. None if the shapes differ."""
    ol, nl = patchlib.walk(old), patchlib.walk(new)

    def keyed(lst):
        seen, res = {}, {}
        for c in lst:
            k = c.co_qualname
            n = seen.get(k, 0)
            seen[k] = n + 1
            res[(k, n)] = c
        return res

    ok, nk = keyed(ol), keyed(nl)
    if set(ok) != set(nk):
        return None
    memo = {}

    def changed(k):
        if k in memo:
            return memo[k]
        oc, nc = ok[k], nk[k]
        r = patchlib.shallow_sig(oc) != patchlib.shallow_sig(nc)
        memo[k] = r
        return r

    # deep: a code object is deep-changed if it or any descendant changed
    out = []
    child_keys = {id(c): k for k, c in ok.items()}

    def rec(oc, nc, key, parent):
        any_child = False
        oi = [c for c in oc.co_consts if hasattr(c, "co_code")]
        ni = [c for c in nc.co_consts if hasattr(c, "co_code")]
        for a, b in zip(oi, ni):
            if rec(a, b, child_keys[id(a)], oc):
                any_child = True
        own = changed(key)
        if own or any_child:
            out.append((oc, nc, own, parent))
            return True
        return False

    rec(old, new, ("<module>", 0), None)
    return out


class Analyzer:
    def __init__(self, root, cmap):
        self.root = root.rstrip("/") + "/"
        self.m = cmap
        self._src = {}

    def src(self, rel):
        if rel not in self._src:
            p = self.root + rel
            if os.path.exists(p):
                with open(p, encoding="utf-8", newline="") as f:
                    self._src[rel] = f.read()
            else:
                self._src[rel] = None
        return self._src[rel]

    def analyze(self, cid, text):
        plan = Plan(cid)
        self._plan = plan
        m = self.m
        try:
            files = patchlib.parse_patch(text)
        except Exception as e:  # unparsable: be conservative
            plan.error = f"parse: {e}"
            return plan
        for f in files:
            old, new = f["old"], f["new"]
            if old is not None:
                base = self.src(old)
                if base is None:
                    plan.error = f"missing {old}"
                    continue
                try:
                    newtext = patchlib.apply_hunks(base, f["hunks"]) if f["hunks"] else base
                except ValueError as e:
                    plan.error = f"apply {old}: {e}"
                    continue
            else:
                newtext = patchlib.apply_hunks("", f["hunks"]) if f["hunks"] else ""
            if new is None:
                plan.files[old] = None
                self.file_change(plan, old, base, None)
            elif old is None:
                plan.files[new] = newtext
                self.file_change(plan, new, None, newtext)
            elif old != new:
                plan.files[old] = None
                plan.files[new] = newtext
                self.file_change(plan, old, base, None)
                self.file_change(plan, new, None, newtext)
            else:
                plan.files[new] = newtext
                self._hunks = f["hunks"]
                self.file_change(plan, new, base, newtext)
                self._hunks = None
        return plan

    def file_change(self, plan, path, txt_old, txt_new):
        m = self.m
        if path.endswith(".py") and path.startswith(PY_ROOTS):
            if txt_old is not None and txt_new is not None:
                self.py_change(plan, path, txt_old, txt_new)
            else:
                # added / deleted / renamed module
                plan.escalate("fresh")
                plan.notes.append(f"{path}: added/removed")
                plan.no_slice = True
                if txt_old is not None:
                    plan.add("file", m.file_dependents(path, include_importers=True))
                if path.startswith("tests/"):
                    plan.add("file", m.tests_in_file(path))
            return
        if path.startswith("tests/") or path in m.opens or path in m.preload_opens:
            # data / fixture file (anywhere in the repo, as long as some test reads it)
            if txt_old is not None and txt_new is not None and txt_old == txt_new:
                return
            if self.class_fixture_change(plan, path, txt_old, txt_new):
                return
            users = m.opens.get(path, set())
            plan.add("fixture", users)
            if path in m.preload_opens:
                plan.escalate("fresh")
                plan.add("fixture", m.preload_open_users(path))
                plan.notes.append(f"{path}: preload-opened -> fresh")
                plan.no_slice = True
            elif users:
                plan.escalate("swap")
                plan.notes.append(f"{path}: opened by {len(users)}")
            else:
                plan.notes.append(f"{path}: never opened")
            return
        if path.startswith("sqlglot/"):
            # non-python file inside the package (e.g. data). Be conservative.
            users = m.opens.get(path, set())
            if users or path in m.preload_opens:
                plan.escalate("fresh")
                plan.add("file", users | m.preload_open_users(path))
            plan.notes.append(f"{path}: package data, users {len(users)}")
            return
        # files outside sqlglot/ and tests/ (docs, CI, benchmarks, ...) cannot affect tests
        plan.notes.append(f"{path}: outside")

    def class_fixture_change(self, plan, path, a, b):
        """A query-pair fixture that a class setup loads into an attribute: only the tests
        reading that attribute see it, and only through the changed pairs (run just those)."""
        if a is None or b is None:
            return False
        if not hasattr(self, "_cfa"):
            self._cfa = class_fixture_attrs(self.root)
        info = self._cfa.get(path)
        if info is None:
            return False
        idx = changed_pairs(a, b)
        if idx is None:
            return False
        attr, readers = info
        m = self.m
        # tests that open the file themselves (outside the class whose setup loads it)
        own = {c.split("::")[1] for c in readers}
        direct = {t for t in m.opens.get(path, set()) if t.split("::")[1] not in own}
        plan.add("fixture", direct)
        plan.add("fixture", readers)
        items = getattr(plan, "fixture_items", None)
        if items is None:
            items = plan.fixture_items = {}
        for t in readers:
            items[t] = (idx, attr)
        plan.escalate("swap")
        plan.notes.append(f"{path}: pairs {idx[:10]} -> {attr} readers {len(readers)}, direct {len(direct)}")
        return True

    def py_change(self, plan, path, a, b):
        m = self.m
        try:
            old = compile(a, self.root + path, "exec", dont_inherit=True)
        except SyntaxError:
            plan.notes.append(f"{path}: original does not compile?")
            plan.no_slice = True
            plan.escalate("fresh")
            plan.add("file", m.file_dependents(path, include_importers=True))
            return
        try:
            new = compile(b, self.root + path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            plan.notes.append(f"{path}: COMPILE ERROR {e.__class__.__name__}")
            plan.no_slice = True
            plan.escalate("fresh")
            plan.add("file", m.file_dependents(path, include_importers=True))
            return
        dc = deep_changed(old, new)
        if dc is None:
            plan.notes.append(f"{path}: shape change")
            plan.no_slice = True
            plan.escalate("fresh")
            plan.add("file", m.file_dependents(path, include_importers=True))
            return
        if not dc:
            plan.notes.append(f"{path}: no bytecode change")
            return
        swaps = []
        for oc, nc, own, parent in dc:
            is_body = oc.co_name == "<module>" or not (oc.co_flags & 0x2)
            key = (path, oc.co_qualname, oc.co_firstlineno)
            if is_body:
                if own:
                    self.body_change(plan, path, oc)
                continue
            swaps.append((oc, nc, own, parent))
            if own:
                plan.add("line", self.line_cands(path, a, oc, m.cover.get(key, set())))
            if own and key in m.setup_keys:
                # runs at "preload" only through a lazy class setup: the child runs that setup
                # eagerly with the swapped code, so its class's tests see the change
                plan.add("file", m.lazy_class_tests())
                plan.notes.append(f"{path}:{oc.co_qualname} runs in a lazy class setup")
            elif own and key in m.preload_keys:
                plan.escalate("fresh")
                plan.notes.append(f"{path}:{oc.co_qualname} runs at preload -> fresh")
                plan.no_slice = True
                # state built at import time may carry the change anywhere
                plan.add("file", m.ids)
            if oc.co_freevars != nc.co_freevars:
                plan.escalate("fresh")
        if swaps:
            plan.escalate("swap")
            plan.swaps.append((path, swaps))
            plan.notes.append(f"{path}: swap {[c[0].co_qualname for c in swaps]}")

    def line_cands(self, path, src, oc, fallback):
        """Tests that execute the statements touched by the patch inside code object `oc`.
        Falls back to function-level coverage when unsure."""
        m = self.m
        hunks = getattr(self, "_hunks", None)
        plan = self._plan
        if not m.has_lines or not hunks:
            plan.touched_exact = False
            return set(fallback)
        stmts = self._stmts(path, src)
        lines = {ln for ln in (l for l, *_ in oc.co_lines() if False)}  # placeholder
        code_lines = {l for _, _, l in oc.co_lines() if l is not None}
        if not code_lines:
            plan.touched_exact = False
            return set(fallback)
        lo, hi = min(code_lines), max(code_lines)
        touched = set()
        src_lines = src.split("\n")

        def stmt_line(ln, step):
            # nearest line in direction `step` that belongs to a statement
            while 1 <= ln <= len(src_lines):
                if self._span(stmts, ln) is not None:
                    return ln
                ln += step
            return None

        for ostart, olen, body, _ in hunks:
            k = ostart if olen > 0 else ostart + 1
            prev = " "
            for tag, _t in body:
                if tag == " ":
                    k += 1
                elif tag == "-":
                    touched.add(k)
                    k += 1
                elif prev == " ":
                    # pure insertion between old lines k-1 and k: neighbouring statements
                    for ln in (stmt_line(k - 1, -1), stmt_line(k, 1)):
                        if ln is not None:
                            touched.add(ln)
                prev = tag if tag != "+" or prev == " " else prev
        touched = {t for t in touched if oc.co_firstlineno <= t <= hi + 1}
        if not touched:
            plan.touched_exact = False
            return set(fallback)
        res = set()
        for t in touched:
            if t <= oc.co_firstlineno or t < lo:
                # def line / decorators / signature: be conservative
                plan.touched_exact = False
                return set(fallback)
            span = self._span(stmts, t)
            if span is None:
                plan.touched_exact = False
                return set(fallback)
            for ln in range(span[0], span[1] + 1):
                res |= m.line_tests(path, ln)
                plan.touched.add((path, ln))
        return res

    def _stmts(self, path, src):
        cache = self.__dict__.setdefault("_stmt_cache", {})
        if path not in cache:
            spans = []
            try:
                tree = ast.parse(src)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, ast.stmt):
                        end = node.end_lineno
                        body = getattr(node, "body", None)
                        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                            # compound statement: header only
                            end = max(node.lineno, body[0].lineno - 1)
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                end = node.lineno - 1 if False else end
                        spans.append((node.lineno, end))
            cache[path] = spans
        return cache[path]

    @staticmethod
    def _span(spans, line):
        best = None
        for a, b in spans:
            if a <= line <= b and (best is None or b - a < best[1] - best[0]):
                best = (a, b)
        return best

    def body_change(self, plan, path, oc):
        m = self.m
        plan.escalate("fresh")
        modname = path[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[: -len(".__init__")]
        plan.slice_mods.add(modname)
        if oc.co_name == "<module>":
            plan.notes.append(f"{path}: module body")
            plan.add("file", m.file_dependents(path, include_importers=True))
            return
        q = oc.co_qualname
        plan.notes.append(f"{path}: class body {q}")
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        tk = f"{mod}:{q}"
        users = m.class_users(tk)
        plan.add("type", users)
        plan.add("type", m.qual_prefix_cover(path, q))
        if m.is_enum(tk) or not users:
            plan.add("file", m.file_dependents(path, include_importers=True))


def _doc(code):
    c = code.co_consts
    if c and isinstance(c[0], str):
        return c[0]
    return None
