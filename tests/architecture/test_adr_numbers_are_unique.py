"""An ADR number names exactly one decision.

This exists because the collision happened. Two branches were open at the same
time in September 2026 and both claimed 0112: `feat/auto-preview-and-project-
memory` (a page shows itself, a project keeps its own note) and
`feat/browser-mcp` (the browser reaches the network only through a guard it
cannot address). Neither branch could see the other's file, both merged
cleanly -- the filenames differ, so git had nothing to report -- and `docs/adr/`
ended up holding two files whose first line said `# ADR-0112`.

**What that costs is not tidiness.** Thirty-five source files, tests and config
comments said `ADR-0112` afterwards, and half of them meant one document and
half the other. A reader following `ADR-0112 §3.5` out of `session.py` lands in
an ADR about `AGENTS.md` and concludes the comment is stale; a reader following
`ADR-0112 §5` out of `status.md` lands in a seccomp argument and concludes the
same. The reference is the only way those comments carry their reasoning, and a
number that resolves to two documents has stopped being a reference.

The README already names this hazard -- "避免三条并行的线各自认领同一个号" under
号段预留 0047-0059 -- and reserving a block is what it offers against it. That
works while people remember to reserve. This test is the half that does not
depend on remembering, and it fails at the moment the second file is added
rather than the moment somebody follows a reference and is misled.

**Two invariants, and the second one is not redundant.** Uniqueness alone is
satisfied by a file renamed to a free number whose title still says the old one
-- which is precisely the half-finished state this test's own fix passes
through. Checking that the title agrees with the filename is what makes the
rename an atomic thing to get right.

**What this deliberately does not check: that every `ADR-NNNN` written anywhere
in the tree resolves to a file here.** ADR-001 through 011 define the baseline
and live in the architecture baseline document, not in this directory; a
resolver test would have to special-case them, and a test with a hard-coded
exemption list for the range it cannot see is a worse guard than none. The
failure this file is about is two documents under one number, and that is
entirely visible from `docs/adr/`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = PROJECT_ROOT / "docs" / "adr"

#: `0112-a-page-shows-itself-and-a-project-keeps-its-own-note.md`. The number is
#: four digits for everything past 0099 and this directory has no three-digit
#: filenames left, so the pattern is exact rather than tolerant -- a file that
#: does not match is reported below rather than skipped.
FILENAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")

#: The first line of an ADR, which looks like this:
#:
#:     `# ADR-0112：写出来的页面自己跑起来`  # noqa: RUF003
#:
#: The colon there is fullwidth because that is what the titles are actually
#: written with; an example that quietly rewrote it would stop being an example
#: of the thing this pattern has to parse.
#:
#: Three or four digits: the older files write `ADR-099` where the newer ones
#: write `ADR-0112`, and both are the number they claim. Compared after
#: zero-padding, not textually.
TITLE = re.compile(r"^#\s*ADR-(\d{3,4})")

#: Not an ADR and never numbered. Listed rather than pattern-matched, so a
#: second unnumbered file has to be added here consciously.
NOT_AN_ADR = frozenset({"README.md"})


def _adr_files() -> tuple[Path, ...]:
    return tuple(
        path for path in sorted(ADR_DIR.glob("*.md")) if path.name not in NOT_AN_ADR
    )


def test_the_listing_is_not_empty() -> None:
    """A guard over nothing would pass forever.

    The same sentence `test_no_stray_duplicates.py` opens with, for the same
    reason: every assertion below is a loop over this listing, so a listing that
    silently came back empty -- a moved directory, a changed suffix -- would
    turn the whole file green.
    """

    assert len(_adr_files()) > 50


def test_every_adr_filename_carries_a_number() -> None:
    """The number is in the filename, so `ls` sorts by decision order."""

    unnumbered = [path.name for path in _adr_files() if not FILENAME.match(path.name)]
    assert unnumbered == [], (
        "these files are in docs/adr/ but are not named `NNNN-slug.md`: "
        f"{unnumbered}. Either give the file its number or add it to "
        "NOT_AN_ADR with a reason."
    )


def test_no_two_adrs_claim_the_same_number() -> None:
    """The invariant the 0112 collision broke."""

    by_number: dict[str, list[str]] = defaultdict(list)
    for path in _adr_files():
        match = FILENAME.match(path.name)
        if match is not None:
            by_number[match.group(1)].append(path.name)

    collisions = {
        number: sorted(names) for number, names in by_number.items() if len(names) > 1
    }
    assert collisions == {}, (
        "two ADRs claim one number, which is how a reference stops resolving: "
        f"{collisions}. The one that landed on `main` first keeps the number "
        "(`git log --diff-filter=A -- docs/adr/<file>`); the later one is "
        "renamed to the next free number, and every `ADR-NNNN` in src/, tests/, "
        "web/src/, docs/, config/, scripts/, docker/ and compose.yaml that "
        "meant it is rewritten in the same commit."
    )


def test_each_adr_title_says_the_number_its_filename_says() -> None:
    """A rename is not finished until the first line agrees with it."""

    disagreements: list[str] = []
    for path in _adr_files():
        match = FILENAME.match(path.name)
        if match is None:
            # Reported by test_every_adr_filename_carries_a_number; this test
            # has nothing to compare against and says so by skipping it rather
            # than failing twice for one cause.
            continue
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        title = TITLE.match(first_line)
        if title is None:
            disagreements.append(
                f"{path.name}: first line does not open `# ADR-NNNN`, it is "
                f"{first_line!r}"
            )
        elif title.group(1).zfill(4) != match.group(1):
            disagreements.append(
                f"{path.name}: title says ADR-{title.group(1)}, filename says "
                f"{match.group(1)}"
            )
    assert disagreements == [], disagreements
