"""Feature switches (for isolating which shortcut causes a miss on the board)."""

OLD_HEAVY = False  # heavy-test probabilities as in 5900057 (>= 3 cheap failures: include)
NO_UNOBS = False  # do not drop heavy tests that state slicing shows cannot observe the change
NO_NEWSLICE = False  # no key-level, module-scoped or fixture-item slicing
# diagnostic: include every test for commits whose 1-based rank (sorted ids) has this bit set
DIAG_BIT = 1
