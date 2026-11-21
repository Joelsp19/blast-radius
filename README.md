# Blast Radius: execute the change, cheaply

Instead of *guessing* which tests a patch breaks, this entry **runs** the tests that can
possibly see the change, and makes that cheap enough to pay for itself.

## Pipeline

1. **Bytecode semantic diff** (`br/patchlib.py`). Each patch is applied in memory and the
   original and patched modules are compiled and compared code object by code object, with
   line tables, docstrings and Python 3.13's `__firstlineno__` normalised away. Comment-only,
   docstring-only and formatting changes compile to identical bytecode and cost 0 seconds.
2. **Coverage maps from the pinned commit** (`br/trace_tests.py`, `br/trace_lines.py`,
   built offline, committed as `br/map.json`). Using `sys.monitoring` we record, per test:
   executed functions and lines, files opened (fixtures), and the runtime classes of
   `self`/`cls` in every method call (so a change to a class attribute such as
   `TSQL.WEEK_OFFSET` maps to the tests that touched a TSQL object, even though no TSQL
   method changed).
3. **Zygote + hot code swapping** (`br/selector.py`). One warm process imports sqlglot and
   the needed test modules once. For every commit it forks a child that patches the changed
   functions *in place* (`function.__code__ = new_code`, Erlang-style hot swap) and runs only
   the candidate tests through unittest (no pytest collection). Changes that cannot be
   swapped (class/module bodies, code run at import) purge sqlglot from the child and
   re-import it from an in-memory overlay. Fixture edits are served through an `open()`
   redirect. Process pools in the tests are replaced by an in-process executor that still
   pickle-round-trips arguments and results, exactly as a spawned worker would. DuckDB
   connections are created lazily so they are fork-safe.
4. **Decision policy** (`br/policy.py`). Running a test costs CPU, including it costs
   simulated time, missing it costs 300 s. Per candidate we choose run / include / skip by
   expected cost; once a commit is known to fail, cheap leftovers ride along in spare
   workers instead of being run. Long tests are never run, only included when evidence
   says they are at risk.
5. **Packing**: failing tests are spread over up to 8 workers minimising
   `max load + 2 s × workers`.

No table from patches or kinds of edits to tests exists anywhere: all maps are built from
the pinned commit only (coverage, opened files, types), as the rules allow.

`tools/` holds the offline tooling (map builder, synthetic mutant generator / labeler used
only for local validation, policy simulator).
