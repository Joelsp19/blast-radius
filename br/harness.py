"""Shared test harness: load sqlglot's tests as unittest cases and run them by number.

Runs tests without pytest (pytest collection costs ~7 CPU seconds); class fixtures
are honoured, and process pools are replaced by an in-process executor so that the
code under test is the code loaded in *this* process.
"""

import concurrent.futures
import importlib
import json
import os
import pickle
import sys
import unittest
from concurrent.futures import Future

SG = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")


def _rt(obj):
    """Pickle round-trip, as a spawned worker would see / return it."""
    return pickle.loads(pickle.dumps(obj))


class SyncExecutor:
    """Drop-in for ProcessPoolExecutor that runs work immediately in-process."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*_rt(initargs))

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            args, kwargs = _rt((args, kwargs))
            f.set_result(_rt(fn(*args, **kwargs)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, **kw):
        return [fn(*a) for a in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def load_tests_json(path):
    with open(path) as f:
        data = json.load(f)
    return data["tests"]


def split_id(tid):
    file, cls, meth = tid.split("::")
    mod = file[:-3].replace("/", ".")
    return file, mod, cls, meth


class LazyConn:
    """duckdb connection that is only opened in the process that first really needs it.

    Statements executed in the zygote (class fixtures) are recorded and replayed, so a
    forked child gets a fresh, fork-safe connection with the same state."""

    def __init__(self, real_connect, args, kwargs):
        self._rc, self._a, self._k = real_connect, args, kwargs
        self._ops = []
        self._real = None
        self._pid = os.getpid()
        self._zygote = _ZYGOTE[0]

    def _get(self):
        if self._real is None:
            self._real = self._rc(*self._a, **self._k)
            for name, a, k in self._ops:
                getattr(self._real, name)(*a, **k)
        return self._real

    def execute(self, *a, **k):
        if self._zygote and os.getpid() == self._pid and self._real is None:
            self._ops.append(("execute", a, k))
            return None
        return self._get().execute(*a, **k)

    def close(self):
        if self._real is not None:
            self._real.close()

    def __enter__(self):
        return self._get().__enter__()

    def __exit__(self, *a):
        return self._get().__exit__(*a)

    def __getattr__(self, name):
        return getattr(self._get(), name)


_ZYGOTE = [False]


def zygote_mode(on=True):
    _ZYGOTE[0] = on


def _patch_duckdb():
    try:
        import duckdb
    except Exception:
        return
    if getattr(duckdb.connect, "_br", False):
        return
    real = duckdb.connect

    def connect(*a, **k):
        # one thread: same results, no thread pool per connection (cheaper, fork-friendly)
        cfg = dict(k.get("config") or {})
        cfg.setdefault("threads", 1)
        k = dict(k, config=cfg)
        if _ZYGOTE[0]:
            return LazyConn(real, a, k)
        return real(*a, **k)

    connect._br = True
    duckdb.connect = connect


def setup_env():
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    _patch_duckdb()
    if SG not in sys.path:
        sys.path.insert(0, SG)
    concurrent.futures.ProcessPoolExecutor = SyncExecutor


class Errored(Exception):
    pass


def import_test_module(mod):
    """Import a test module; return (module, None) or (None, exception)."""
    try:
        m = importlib.import_module(mod)
    except BaseException as e:  # noqa
        return None, e
    if hasattr(m, "ProcessPoolExecutor"):
        m.ProcessPoolExecutor = SyncExecutor
    return m, None


_class_state = {}  # cls -> None (ok) or exception


# class fixtures that are expensive and only used by a few (heavy) tests: built on first use
LAZY_SETUP = {
    ("tests.test_executor", "TestExecutor"): (
        "tpch_conn", "tpcds_conn", "tpch_tables", "tpcds_tables", "cache", "tpch_sqls", "tpcds_sqls"),
}


class _LazyAttr:
    def __init__(self, name):
        self.name = name

    def __get__(self, obj, owner):
        for n in LAZY_SETUP[(owner.__module__, owner.__name__)]:
            if isinstance(owner.__dict__.get(n), _LazyAttr):
                delattr(owner, n)
        owner.setUpClass()  # assigns the real attributes
        return getattr(owner, self.name)


def class_setup(cls):
    """Run setUpClass once (lazily). Returns None or the exception."""
    if cls in _class_state:
        return _class_state[cls]
    lazy = LAZY_SETUP.get((cls.__module__, cls.__name__))
    if lazy and not getattr(cls, "__unittest_skip__", False):
        for n in lazy:
            setattr(cls, n, _LazyAttr(n))
        _class_state[cls] = None
        return None
    err = None
    if getattr(cls, "__unittest_skip__", False):
        _class_state[cls] = None
        return None
    try:
        cls.setUpClass()
        if hasattr(cls, "pool"):
            try:
                cls.pool.shutdown(wait=False)
            except Exception:
                pass
            cls.pool = SyncExecutor()
    except BaseException as e:  # noqa
        err = e
    _class_state[cls] = err
    return err


def run_case(cls, meth):
    """Run one test method. Returns True if it failed (failure/error/subtest failure)."""
    err = class_setup(cls)
    if err is not None:
        return True
    case = cls(meth)
    result = unittest.TestResult()
    case.run(result)
    if result.failures or result.errors or result.unexpectedSuccesses:
        return True
    return False


_mods = {}


def get_class(tid):
    file, mod, cls, meth = split_id(tid)
    m = _mods.get(mod)
    if m is None:
        m, err = import_test_module(mod)
        _mods[mod] = m
    if m is None:
        return None, meth
    return getattr(m, cls, None), meth


def preload(ids, dialects=True, setups=True):
    """Import sqlglot (+all dialects), the test modules of `ids`, and run their class setups."""
    import sqlglot  # noqa

    if dialects:
        from sqlglot.dialects import Dialects
        from sqlglot.dialects.dialect import Dialect

        for d in Dialects:
            try:
                Dialect.get_or_raise(d.value)
            except Exception:
                pass
    for tid in ids:
        klass, meth = get_class(tid)
        if setups and klass is not None:
            class_setup(klass)


def run_id(tid, subset=None, attr=None):
    klass, meth = get_class(tid)
    if klass is None:
        return True
    if subset is None:
        return run_case(klass, meth)
    if class_setup(klass) is not None:
        return True
    full = getattr(klass, attr)
    setattr(klass, attr, [full[i] for i in subset])
    try:
        return run_case(klass, meth)
    finally:
        setattr(klass, attr, full)
