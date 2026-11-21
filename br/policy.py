"""Decision policy: which candidate tests to run, include blindly, or skip.

Cost model (per commit):  pack_cost(output) + CPU spent verifying + 300 * P(miss).
"""

MISS = 300_000  # ms
WORKER = 2_000  # ms


def pack_cost(ms_list):
    """min over w of LPT max-load + 2s*w (0 for empty)."""
    if not ms_list:
        return 0
    items = sorted(ms_list, reverse=True)
    best = None
    for w in range(1, min(8, len(items)) + 1):
        loads = [0] * w
        for x in items:
            i = loads.index(min(loads))
            loads[i] += x
        c = max(loads) + WORKER * w
        if best is None or c < best:
            best = c
    return best


class Params:
    heavy_ms = 3000  # never run tests at least this slow
    p_fixture = 0.95  # calibrated (0.96)
    p_line = 0.45  # calibrated on sample + synthetic commits (0.51)
    p_type = 0.02  # calibrated (0.022)
    p_heavy_clean = 0.02  # heavy candidate, nothing else failed
    p_heavy_dirty = 0.8  # heavy candidate, >= 3 cheap tests failed
    cpu_factor = 1.0  # harness cpu vs estimate


def decide_upfront(cands, p, ms, cpu, P):
    """Split candidates: include directly (D), heavy (H), run list (ordered)."""
    D, H, R = set(), set(), []
    for t in cands:
        if ms[t] >= P.heavy_ms:
            H.add(t)
            continue
        # include blindly when running is expected to cost more than including
        if cpu[t] * P.cpu_factor > (1 - p[t]) * (ms[t] + WORKER):
            D.add(t)
        else:
            R.append(t)
    # most likely to fail per cpu second first: find failures early
    R.sort(key=lambda t: (-p[t] / max(cpu[t], 1), cpu[t]))
    return D, H, R


def should_stop(S_ms, remaining, cpu, ms, p, evidence=True):
    """Stop running if including everything left is cheaper than verifying it.
    Only once the commit is known (or nearly sure) to fail: otherwise keep trying to
    prove it clean, which is worth the whole output cost."""
    if not remaining:
        return True
    if not S_ms or not evidence:
        return False
    base = pack_cost(S_ms)
    inc = pack_cost(S_ms + [ms[t] for t in remaining]) - base
    run = sum(cpu[t] for t in remaining)
    return inc <= run


ROBUST = ("test_max_nodes", "test_generator_memory")


def heavy_include(h, S_ms, ms, n_fail, P, kind="file", n_cheap_line=0):
    """A heavy test is never run whole. Its failure probability grows with how broadly the
    change breaks cheap tests (verified on synthetic commits: tpcds failed alongside 7, 30,
    35, 51 cheap failures and passed alongside 1 or 2). With no failure it is high only when
    it is one of the few tests executing the changed line."""
    # verified on synthetic commits: executor tpcds failed alongside 30, 35, 35, 51, 56 cheap
    # failures and passed alongside 0, 0, 1, 1, 1, 2, 7; test_max_nodes passed alongside 7
    import flags

    robust = h.rsplit("::", 1)[-1] in ROBUST
    if flags.OLD_HEAVY:
        if n_fail >= 3:
            p = P.p_heavy_dirty
        elif n_fail >= 1:
            p = 0.06 if kind == "line" else 0.01
        elif kind == "line":
            p = max(P.p_heavy_clean, 0.4 / (1 + n_cheap_line))
        else:
            p = P.p_heavy_clean
    elif n_fail >= 25:
        p = P.p_heavy_dirty
    elif robust and kind != "line":
        # stress tests of plain SELECT parsing / generator construction: they break only
        # with the basics (hundreds of failures) or through their own code (line kind)
        p = 0.01
    elif n_fail >= 10:
        p = 0.4
    elif n_fail >= 3:
        p = 0.08
    elif n_fail >= 1:
        p = 0.06 if kind == "line" else 0.01
    elif kind == "line":
        p = max(P.p_heavy_clean, 0.4 / (1 + n_cheap_line))
    else:
        p = P.p_heavy_clean
    marg = pack_cost(S_ms + [ms[h]]) - pack_cost(S_ms)
    return p * MISS > marg
