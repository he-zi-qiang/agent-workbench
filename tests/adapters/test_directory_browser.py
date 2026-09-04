"""Where the folder picker opens (ADR-074, ADR-0109).

Two answers, and the second only exists because of a container: the user's
home is right when the process runs as the user on the user's machine, and
wrong when the process runs in an image whose home is its own read-only tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_workbench.adapters.filesystem.browser import FilesystemDirectoryBrowser


def test_without_a_configured_root_the_picker_opens_at_home() -> None:
    assert FilesystemDirectoryBrowser().home() == str(Path.home())


@pytest.mark.anyio
async def test_a_configured_root_is_where_browsing_starts(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    browser = FilesystemDirectoryBrowser(start=str(tmp_path))

    assert browser.home() == str(tmp_path)
    listing = await browser.browse()
    assert listing.path == str(tmp_path.resolve())
    assert [entry.name for entry in listing.entries] == ["alpha"]


def test_a_configured_root_that_is_not_a_directory_falls_back_to_home(
    tmp_path: Path,
) -> None:
    """A topology edited to drop the mount must not turn into a 400 on load.

    The picker's first request is `browse(None)`. Refusing it would show a
    person an error before they had chosen anything; the home directory is
    the same picker the native path shows.
    """

    browser = FilesystemDirectoryBrowser(start=str(tmp_path / "never-mounted"))
    assert browser.home() == str(Path.home())
