"""Runtime view of the coverage map built at the pinned commit (see tools/build_map.py).

Tests are referred to by their pytest id string."""

import json


class _LazySets:
    """dict-like: key -> frozenset of test ids, built on first access."""

    def __init__(self, ids, raw):
        self.ids, self.raw, self.done = ids, raw, {}

    def get(self, k, default=None):
        r = self.done.get(k)
        if r is None:
            lst = self.raw.get(k)
            if lst is None:
                return default
            r = self.done[k] = frozenset(self.ids[i] for i in lst)
        return r

    def __getitem__(self, k):
        r = self.get(k)
        if r is None:
            raise KeyError(k)
        return r

    def __contains__(self, k):
        return k in self.raw


class CMap:
    def __init__(self, path):
        if path.endswith(".gz"):
            import gzip

            with gzip.open(path, "rb") as f:
                d = json.loads(f.read())
        else:
            with open(path) as f:
                d = json.load(f)
        ids = d["tests"]
        self.ids = ids
        S = lambda lst: frozenset(ids[i] for i in lst)  # noqa
        self.cover = _LazySets(ids, {(k[0], k[1], k[2]): lst for k, lst in zip(d["keys"], d["cover"])})
        self.keys_by_file = {}
        for k in d["keys"]:
            self.keys_by_file.setdefault(k[0], []).append((k[0], k[1], k[2]))
        self.opens = _LazySets(ids, d["opens"])
        self.preload_opens = set(d["preload_opens"])
        self._pou = {p: S(v) for p, v in d.get("preload_open_users", {}).items()}
        self.preload_keys = {tuple(d["keys"][i]) for i in d["preload_keys"]}
        self.types = _LazySets(ids, d["types"])
        self.mro = d["mro"]
        self.enums = set(d.get("enums", []))
        self.importers = d.get("importers", {})  # module relpath -> [test files]
        self.mod_importers = d.get("mod_importers", {})  # relpath -> [sqlglot relpaths importing it]
        self.rclosure = d.get("rclosure", {})  # relpath -> all modules importing it (transitively)
        self.by_file = {}
        for t in ids:
            self.by_file.setdefault(t.split("::")[0], set()).add(t)
        self.doc_tests = frozenset(t for t in ids if t.startswith("tests/test_docs.py::"))
        self.cpu = dict(zip(ids, d.get("cpu", [0] * len(ids))))
        self._fc = {}
        self.subtests = d.get("subtests", {})
        self._d = {k: d[k] for k in ("attr_map", "attr_sets") if k in d}
        self.key_labels = set(d.get("key_labels", ()))
        self.key_unsafe = set(d.get("key_unsafe", ()))
        self._kw = d.get("key_whole", {})
        self._kk = d.get("key_keys", {})
        self._ks = d.get("key_sets", [])
        self.has_lines = "lines" in d
        self._sets = d.get("line_sets", [])
        self._lines = d.get("lines", {})
        self._set_cache = {}

    def sub_queries(self, tid, touched):
        """Indices of the sub-test queries of `tid` executing any (path, line) in touched,
        or None if `tid` cannot be sliced."""
        st = self.subtests.get(tid)
        if st is None or "base" in st:
            return None
        q = set()
        for path, ln in touched:
            q.update(st["lines"].get(path, {}).get(str(ln), ()))
        return sorted(q)

    def key_readers(self, label, changed_keys):
        """Tests that observed container `label` as a whole or looked up a changed key;
        None if the container was not traced (or could be read around the recorder)."""
        if label not in self.key_labels or label in self.key_unsafe:
            return None
        idx = set()
        w = self._kw.get(label)
        if w is not None:
            idx.update(self._ks[w])
        kk = self._kk.get(label, {})
        for k in changed_keys:
            j = kk.get(k)
            if j is not None:
                idx.update(self._ks[j])
        return {self.ids[i] for i in idx}

    def fixture_slice(self, tid, touched):
        """Fixture-item slicing: (items executing any touched line, base hits?) or None."""
        st = self.subtests.get(tid)
        if st is None or "base" not in st:
            return None
        q = set()
        base_hit = False
        for path, ln in touched:
            q.update(st["lines"].get(path, {}).get(str(ln), ()))
            if not base_hit:
                bl = st.get("_bs", {}).get(path)
                if bl is None:
                    bl = st.setdefault("_bs", {})[path] = set(st["base"].get(path, ()))
                base_hit = ln in bl
        return sorted(q), base_hit

    @property
    def has_attrs(self):
        return "attr_map" in self._d

    def attr_readers(self, name):
        """Tests that loaded attribute/global `name` (empty if none did)."""
        si = self._d["attr_map"].get(name)
        if si is None:
            return frozenset()
        key = ("a", si)
        r = self._set_cache.get(key)
        if r is None:
            r = self._set_cache[key] = frozenset(self.ids[i] for i in self._d["attr_sets"][si])
        return r

    def line_files(self):
        return list(self._lines)

    def line_tests(self, path, line):
        si = self._lines.get(path, {}).get(str(line))
        if si is None:
            return frozenset()
        r = self._set_cache.get(si)
        if r is None:
            r = self._set_cache[si] = frozenset(self.ids[i] for i in self._sets[si])
        return r

    def tests_in_file(self, path):
        return set(self.by_file.get(path, ()))

    def file_cover(self, path):
        r = self._fc.get(path)
        if r is None:
            r = set()
            for k in self.keys_by_file.get(path, ()):
                r |= self.cover[k]
            self._fc[path] = r
        return r

    def class_users(self, tk):
        r = set()
        for t, bases in self.mro.items():
            if tk in bases and t in self.types:
                r |= self.types[t]
        return r

    def module_types_users(self, path):
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        r = set()
        for t, bases in self.mro.items():
            for b in bases:
                if b.split(":", 1)[0] == mod:
                    r |= self.types.get(t, set())
                    break
        return r

    def qual_prefix_cover(self, path, q):
        r = set()
        pre = q + "."
        for k in self.keys_by_file.get(path, ()):
            if k[1] == q or k[1].startswith(pre):
                r |= self.cover[k]
        return r

    def is_enum(self, tk):
        return tk in self.enums

    def preload_open_users(self, path):
        return set(self._pou.get(path, ()))

    def file_dependents(self, path, include_importers=True):
        r = set(self.file_cover(path))
        r |= self.module_types_users(path)
        if path.startswith("tests/"):
            r |= self.tests_in_file(path)
        if include_importers:
            for tf in self.importers.get(path, ()):
                r |= self.tests_in_file(tf)
            for mp in self.mod_importers.get(path, ()):
                r |= self.file_cover(mp)
                r |= self.module_types_users(mp)
        return r
