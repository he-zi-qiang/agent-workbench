"""No file whose name is an accident.

Editors and file-syncing tools produce copies named "thing 2.py" beside
"thing.py". One of those reached this repository: a snapshot of an adapter and
its tests taken before a fix, swept in by ``git add -A``. The test copy was
collected and passed -- it tested an older version of code that still worked --
so nothing failed and the suite quietly reported a larger number than it had
distinct tests.

That is the failure worth guarding: not the wasted bytes, but a duplicate that
runs, agrees, and hides that two versions of the same file are in the tree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# "thing 2.py", "thing copy.py", "thing (1).py" -- the shapes a duplicate
# arrives under. A deliberate name would not need any of them.
DUPLICATE_SHAPES = re.compile(r"( \d+| copy| \(\d+\))\.[A-Za-z0-9]+$")


def _tracked_files() -> tuple[str, ...]:
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(line for line in listing.stdout.splitlines() if line)


def test_the_listing_is_not_empty() -> None:
    """A guard over nothing would pass forever."""

    assert len(_tracked_files()) > 50


def test_no_tracked_file_looks_like_an_editor_duplicate() -> None:
    strays = sorted(name for name in _tracked_files() if DUPLICATE_SHAPES.search(name))

    assert not strays, (
        "these look like duplicates a tool made rather than names somebody "
        "chose:\n" + "\n".join(f"  {name}" for name in strays)
    )


def test_the_pattern_recognises_the_shapes_it_is_for() -> None:
    """Otherwise the guard above could be matching nothing at all."""

    for name in ("src/thing 2.py", "docs/notes copy.md", "tests/t (1).py"):
        assert DUPLICATE_SHAPES.search(name), name
    for name in ("src/thing.py", "tests/test_2fa.py", "docs/adr/0012-x.md"):
        assert not DUPLICATE_SHAPES.search(name), name
