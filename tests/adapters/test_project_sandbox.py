"""The escapes only a real filesystem can attempt.

Every case here is one the lexical half passes. That is the point of the file:
if a test in it can be made to pass by editing ``domain/project_files.py``, it
belongs in ``tests/domain/test_project_files.py`` instead, and this file has
stopped testing what it exists to test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_workbench.adapters.filesystem.sandbox import (
    ProjectSandbox,
    ProjectSandboxError,
)
from agent_workbench.domain.project_files import (
    ProjectPathError,
    validate_relative_path,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "id_ed25519").write_text("PRIVATE KEY\n")
    return secret


class TestSymlinkEscape:
    """The reason this half of the sandbox exists."""

    def test_a_symlinked_directory_cannot_be_read_through(
        self, project: Path, outside: Path
    ) -> None:
        # Every segment of `notes/id_ed25519` is lexically innocent, and the
        # lexical half says so -- asserted here so the test cannot quietly
        # become a duplicate of a domain test.
        (project / "notes").symlink_to(outside)
        assert validate_relative_path("notes/id_ed25519")

        sandbox = ProjectSandbox(project)
        with pytest.raises(ProjectSandboxError, match="escapes the project root"):
            sandbox.resolve("notes/id_ed25519")

    def test_a_symlink_chain_cannot_be_read_through(
        self, project: Path, outside: Path
    ) -> None:
        # Two hops. `resolve()` follows the whole chain, so one check catches
        # an escape that a single-level `is_symlink()` inspection would miss.
        (project / "hop").symlink_to(outside)
        (project / "notes").symlink_to(project / "hop")
        sandbox = ProjectSandbox(project)
        with pytest.raises(ProjectSandboxError, match="escapes"):
            sandbox.resolve("notes/id_ed25519")

    def test_a_symlink_that_stays_inside_is_allowed(self, project: Path) -> None:
        # The sandbox refuses escapes, not symlinks. A link that lands back
        # inside the root is an ordinary part of a source tree (node_modules
        # and .venv are full of them) and refusing it would make the sandbox
        # unusable on real projects.
        (project / "alias").symlink_to(project / "src")
        sandbox = ProjectSandbox(project)
        assert (
            sandbox.resolve("alias/main.py") == (project / "src" / "main.py").resolve()
        )

    def test_the_leaf_symlink_is_refused_on_write_even_though_it_resolves_inside(
        self, project: Path
    ) -> None:
        # `inside_link -> src/main.py` resolves within the root, so `resolve()`
        # is right to allow it. O_NOFOLLOW still refuses the write: the caller
        # asked to write `inside_link`, and writing *through* it silently
        # modifies a different file than the one named.
        (project / "inside_link").symlink_to(project / "src" / "main.py")
        sandbox = ProjectSandbox(project)
        assert sandbox.resolve("inside_link")
        with pytest.raises(ProjectSandboxError, match="symlink"):
            sandbox.open_for_write("inside_link")

    def test_a_leaf_symlink_pointing_outside_is_refused_on_write(
        self, project: Path, outside: Path
    ) -> None:
        # The escape this most directly prevents: plant a link, then write.
        (project / "escape").symlink_to(outside / "id_ed25519")
        sandbox = ProjectSandbox(project)
        with pytest.raises(ProjectSandboxError):
            sandbox.open_for_write("escape")
        assert (outside / "id_ed25519").read_text() == "PRIVATE KEY\n"

    def test_a_leaf_symlink_is_refused_on_read(
        self, project: Path, outside: Path
    ) -> None:
        # Refused as an escape, not as a symlink: this link points outside, so
        # containment catches it before the open flag is reached. Both refusals
        # are correct and the order is not arbitrary -- the cheaper, more
        # specific reason wins, and the caller learns the path left the project
        # rather than the mechanism by which it tried to.
        (project / "escape").symlink_to(outside / "id_ed25519")
        sandbox = ProjectSandbox(project)
        with pytest.raises(ProjectSandboxError, match="escapes"):
            sandbox.read_bytes("escape")

    def test_a_leaf_symlink_staying_inside_is_still_refused_on_read(
        self, project: Path
    ) -> None:
        # The case where containment *cannot* help, so O_NOFOLLOW is the only
        # thing refusing. Reading `alias_file` must not silently hand back the
        # contents of a different file.
        (project / "alias_file").symlink_to(project / "src" / "main.py")
        sandbox = ProjectSandbox(project)
        with pytest.raises(ProjectSandboxError, match="symlink"):
            sandbox.read_bytes("alias_file")


class TestRootItself:
    def test_a_relative_root_is_refused(self, project: Path) -> None:
        with pytest.raises(ProjectSandboxError, match="absolute"):
            ProjectSandbox("project")

    def test_a_missing_root_is_refused_at_construction(self, tmp_path: Path) -> None:
        # At construction, not on first use: a sandbox over a root that is not
        # there is a deployment that looks healthy until an agent hits it.
        with pytest.raises(ProjectSandboxError, match="existing directory"):
            ProjectSandbox(tmp_path / "nope")

    def test_a_file_is_not_a_root(self, project: Path) -> None:
        with pytest.raises(ProjectSandboxError, match="existing directory"):
            ProjectSandbox(project / "src" / "main.py")

    def test_a_symlinked_root_is_resolved_and_still_works(
        self, project: Path, tmp_path: Path
    ) -> None:
        # Registering `/link` where `/link -> /real` must not make every path
        # inside it read as an escape. The root is resolved once at
        # construction, so the comparison is real-path against real-path.
        link = tmp_path / "link"
        link.symlink_to(project)
        sandbox = ProjectSandbox(link)
        assert sandbox.root == project.resolve()
        assert sandbox.resolve("src/main.py") == (project / "src" / "main.py").resolve()

    def test_a_sibling_whose_name_is_a_prefix_is_outside(self, tmp_path: Path) -> None:
        # The `startswith` bug, end to end rather than in the pure helper.
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha-secrets").mkdir()
        (tmp_path / "alpha-secrets" / "creds").write_text("x")
        sandbox = ProjectSandbox(tmp_path / "alpha")
        (tmp_path / "alpha" / "peek").symlink_to(tmp_path / "alpha-secrets")
        with pytest.raises(ProjectSandboxError, match="escapes"):
            sandbox.resolve("peek/creds")


class TestOrdinaryUse:
    """A sandbox that refuses real work is not a sandbox, it is a wall."""

    def test_reading_a_file(self, project: Path) -> None:
        sandbox = ProjectSandbox(project)
        assert sandbox.read_bytes("src/main.py") == b"print('hi')\n"

    def test_writing_creates_parent_directories(self, project: Path) -> None:
        # The capability the flat workspace could not offer, and the reason
        # ADR-072 exists: an agent has to be able to make `a/b/c/`.
        sandbox = ProjectSandbox(project)
        descriptor = sandbox.open_for_write("docs/adr/0072.md")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"# ADR-072\n")
        assert (project / "docs" / "adr" / "0072.md").read_text() == "# ADR-072\n"

    def test_exclusive_write_refuses_an_existing_file(self, project: Path) -> None:
        sandbox = ProjectSandbox(project)
        with pytest.raises(FileExistsError):
            sandbox.open_for_write("src/main.py", exist_ok=False)

    def test_a_lexically_bad_path_is_refused_before_any_io(self, project: Path) -> None:
        # Ordering, asserted: the physical check must never run on a string
        # containing a NUL, because os.open raises ValueError rather than the
        # sandbox's own error and the caller would see the wrong failure.
        sandbox = ProjectSandbox(project)
        # `ProjectPathError`, not `ProjectSandboxError`: the lexical half raises
        # the base class, and the sandbox error is a *subclass* of it, so
        # catching the subclass here would not catch the parent. Asserted in the
        # direction a caller actually writes it -- one `except ProjectPathError`
        # covers both halves, which is why the subclass relationship is that way
        # round.
        with pytest.raises(ProjectPathError, match="NUL"):
            sandbox.resolve("a\x00b")
        with pytest.raises(ProjectPathError, match="\\.\\."):
            sandbox.resolve("../../etc/passwd")

    def test_a_cjk_path_round_trips(self, project: Path) -> None:
        # This repository's own directory is `agent工作台`. A sandbox that
        # mangles CJK is one that cannot be pointed at this checkout.
        sandbox = ProjectSandbox(project)
        descriptor = sandbox.open_for_write("文档/设计稿.md")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write("稿子\n".encode())
        assert sandbox.read_bytes("文档/设计稿.md") == "稿子\n".encode()
