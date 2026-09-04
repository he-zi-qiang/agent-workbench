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


# --- making one (ADR-074 said "choose a folder, or make one") -----------------


@pytest.mark.anyio
async def test_creating_makes_one_empty_folder_and_names_it_back(
    tmp_path: Path,
) -> None:
    browser = FilesystemDirectoryBrowser()

    made = await browser.create(str(tmp_path), "  notes ")

    # Trimmed, and reported with the absolute path the picker's next request
    # will be -- the same rule as every entry `browse` returns.
    assert made.name == "notes"
    assert made.path == str(tmp_path / "notes")
    assert (tmp_path / "notes").is_dir()
    assert list((tmp_path / "notes").iterdir()) == []
    listing = await browser.browse(str(tmp_path))
    assert [entry.name for entry in listing.entries] == ["notes"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name",
    ["", "   ", ".", "..", "a/b", "a\\b", "../escape", "x\0y", "n" * 256],
)
async def test_a_name_that_is_not_one_segment_is_refused_before_the_disk(
    tmp_path: Path, name: str
) -> None:
    """The picker builds exactly one path, and it points directly inside `parent`.

    Refused lexically so a name like `../escape` cannot become a directory one
    level up: `mkdir` would happily do that, and the person would then be
    offered a folder outside the tree they were browsing.
    """

    from agent_workbench.domain.project_files import ProjectPathError

    browser = FilesystemDirectoryBrowser()
    before = sorted(tmp_path.iterdir())
    with pytest.raises(ProjectPathError):
        await browser.create(str(tmp_path), name)
    assert sorted(tmp_path.iterdir()) == before
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.anyio
async def test_an_existing_name_is_its_own_error_so_the_route_can_say_409(
    tmp_path: Path,
) -> None:
    from agent_workbench.domain.project_files import (
        DirectoryExistsError,
        ProjectPathError,
    )

    (tmp_path / "taken").mkdir()
    browser = FilesystemDirectoryBrowser()

    with pytest.raises(DirectoryExistsError) as caught:
        await browser.create(str(tmp_path), "taken")
    # Still a ProjectPathError, so a caller that only knows the parent class
    # keeps working; the subclass is for the one caller that wants to differ.
    assert isinstance(caught.value, ProjectPathError)


@pytest.mark.anyio
async def test_a_parent_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    from agent_workbench.domain.project_files import ProjectPathError

    (tmp_path / "a-file.txt").write_text("x")
    browser = FilesystemDirectoryBrowser()

    with pytest.raises(ProjectPathError):
        await browser.create(str(tmp_path / "a-file.txt"), "child")
    with pytest.raises(ProjectPathError):
        await browser.create("relative/parent", "child")
