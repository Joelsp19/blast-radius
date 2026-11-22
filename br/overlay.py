"""Make a process see a patched sqlglot checkout without touching the read-only disk.

- .py files under the checkout are served from memory by a meta-path finder
  (patched / added / deleted modules), keeping the real path as __file__.
- other files (fixtures, data) are redirected at open() time to copies in /tmp.
"""

import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import io
import os
import sys


class _Loader(importlib.abc.Loader):
    def __init__(self, fullname, path, source, is_pkg):
        self.fullname, self.path, self.source, self.is_pkg = fullname, path, source, is_pkg

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if self.source is None:
            raise ModuleNotFoundError(f"No module named {self.fullname!r}", name=self.fullname)
        code = compile(self.source.encode("utf-8", "surrogateescape"), self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_source(self, fullname):
        return self.source

    def is_package(self, fullname):
        return self.is_pkg


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, files):
        self.root = root.rstrip("/") + "/"
        self.files = files  # relpath -> new text or None (deleted)

    def find_spec(self, fullname, path=None, target=None):
        rel = fullname.replace(".", "/")
        for cand, is_pkg in ((rel + ".py", False), (rel + "/__init__.py", True)):
            if cand in self.files:
                full = self.root + cand
                src = self.files[cand]
                loader = _Loader(fullname, full, src, is_pkg)
                spec = importlib.util.spec_from_loader(fullname, loader, origin=full, is_package=is_pkg)
                if src is not None:
                    spec.has_location = True
                    if is_pkg:
                        spec.submodule_search_locations = [os.path.dirname(full)]
                return spec
        return None


_real_open = builtins.open


def install(root, files, tmpdir):
    """files: relpath -> new text (or None for deleted)."""
    pyfiles = {k: v for k, v in files.items() if k.endswith(".py")}
    if pyfiles:
        sys.meta_path.insert(0, _Finder(root, pyfiles))
    others = {k: v for k, v in files.items() if not k.endswith(".py")}
    if not others:
        return
    redirect = {}
    root = root.rstrip("/") + "/"
    for rel, text in others.items():
        full = root + rel
        if text is None:
            redirect[full] = None
            continue
        dst = os.path.join(tmpdir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with _real_open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        redirect[full] = dst

    def _map(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.fsdecode(file)
            if not os.path.isabs(p):
                p = os.path.abspath(p)
            p = os.path.normpath(p)
            if p in redirect:
                r = redirect[p]
                if r is None:
                    raise FileNotFoundError(2, "No such file or directory", p)
                return r
        return file

    def patched_open(file, *a, **k):
        return _real_open(_map(file), *a, **k)

    builtins.open = patched_open
    io.open = patched_open
