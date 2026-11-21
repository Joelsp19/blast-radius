"""Feature switches (for isolating which shortcut causes a miss on the board)."""

OLD_HEAVY = True  # heavy-test probabilities as in 5900057 (>= 3 cheap failures: include)
NO_UNOBS = True  # do not drop heavy tests that state slicing shows cannot observe the change
NO_NEWSLICE = False  # no key-level, module-scoped or fixture-item slicing
