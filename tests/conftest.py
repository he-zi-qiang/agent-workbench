"""Collection rules for the whole suite.

The only thing here is a refusal to collect duplicate copies of test files.
``thing 2.py`` beside ``thing.py`` is what an editor or a syncing file system
leaves behind, and pytest's default ``test_*.py`` glob matches it: the copy is
collected, runs an older version of code that usually still works, and passes.
Nothing fails, and the suite reports a number larger than the count of distinct
tests it has -- which is the worst outcome, because the number is the evidence.

Measured once, on a machine whose ``~/Documents`` is synced: 232 of 1829
collected tests came from copies. Ignoring them is not tidiness; it is the
difference between a test count that means something and one that does not.

The copies are still an error, and ``tests/architecture/test_no_stray_duplicates``
is where they are reported. Ignored here so a run is trustworthy, named there so
nobody has to notice on their own.
"""

from __future__ import annotations

#: " 2.py", " 3.py" and so on -- the shape a duplicate arrives under. Narrow on
#: purpose: this repository has no tracked file with a space in its name, and a
#: deliberately named test would not need one.
collect_ignore_glob = ["*[ ][0-9].py", "*[ ][0-9][0-9].py", "*[ ]copy.py"]
