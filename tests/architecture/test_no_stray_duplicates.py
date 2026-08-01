"""No file whose name is an accident.

Editors and file-syncing tools produce copies named "thing 2.py" beside
"thing.py". One of those reached this repository: a snapshot of an adapter and
its tests taken before a fix, swept in by ``git add -A``. The test copy was
collected and passed -- it tested an older version of code that still worked --
so nothing failed and the suite quietly reported a larger number than it had
distinct tests.

That is the failure worth guarding: not the wasted bytes, but a duplicate that
runs, agrees, and hides that two versions of the same file are in the tree.

Tracked files were the whole of that guard, and they are not the whole of the
problem. On a machine whose ``~/Documents`` is synced, these copies appear
*untracked* and keep appearing -- thirty of them in three days -- and two of the
three ways they do damage never involve git at all. Both of the others now have
a defence that does not depend on anybody noticing:

* pytest is told not to collect them, in ``tests/conftest.py``. Left alone they
  are collected and pass, which inflates the reported count with copies of tests
  that already ran -- the same "quietly reported a larger number" above, from a
  file git never sees.
* ``migrations/versions`` is checked below, because alembic has no way to be
  told to skip a file. A stray copy there declares a revision id that already
  exists, and alembic reports *multiple heads* -- an error that says nothing
  about the cause and stops every migration until somebody works it out.

``.gitignore`` covers the third: they can no longer be swept in by ``git add
-A``, which is how the first one arrived.
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


def test_no_stray_revision_file_sits_beside_the_real_migrations() -> None:
    """Checked on disk, not through git: an untracked copy breaks alembic too.

    A duplicate here declares a revision id that already exists, and what alembic
    then reports is "multiple head revisions" -- which names neither the file nor
    the cause, and blocks every migration until somebody finds it. This says
    which file, and why.
    """

    versions = PROJECT_ROOT / "migrations" / "versions"
    assert versions.is_dir(), "the migration directory moved; this guard is stale"

    strays = sorted(
        path.name
        for path in versions.glob("*.py")
        if DUPLICATE_SHAPES.search(path.name)
    )

    assert not strays, (
        "these are copies sitting beside the real migrations. Alembic loads "
        "every .py here, so each one re-declares a revision id that already "
        "exists and turns `alembic heads` into 'multiple head revisions'. "
        "Delete them:\n" + "\n".join(f"  migrations/versions/{name}" for name in strays)
    )


def test_the_pattern_recognises_the_shapes_it_is_for() -> None:
    """Otherwise the guards above could be matching nothing at all."""

    for name in ("src/thing 2.py", "docs/notes copy.md", "tests/t (1).py"):
        assert DUPLICATE_SHAPES.search(name), name
    for name in ("src/thing.py", "tests/test_2fa.py", "docs/adr/0012-x.md"):
        assert not DUPLICATE_SHAPES.search(name), name
