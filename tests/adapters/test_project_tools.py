"""The project-directory tools, and the exclusivity that makes them safe.

The most important test in this file is the shortest one: the two tool sets are
disjoint. Everything else here checks that a tool does what it says; that one
checks that a model is never in a position to have to choose between two tools
that both say "write a file" (ADR-073 §2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_workbench.adapters.filesystem.project_files import (
    FilesystemProjectFileStore,
)
from agent_workbench.adapters.filesystem.sandbox import ProjectSandbox
from agent_workbench.adapters.tools.project_files import (
    ProjectEditTool,
    ProjectFilesUnavailableError,
    ProjectListTool,
    ProjectReadTool,
    ProjectWriteTool,
)
from agent_workbench.application.code_session import (
    CODE_PROJECT_TOOLS,
    CODE_PROJECT_TOOLS_WITH_SANDBOX,
    CODE_TOOLS,
    CODE_TOOLS_WITH_SANDBOX,
    CodeSessionService,
)
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation

_UNSET: object = object()

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# alpha\n")
    return root


@pytest.fixture
def scope(project: Path) -> ProjectFileScope:
    return ProjectFileScope()


def _invocation(name: str, **arguments: object) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(
            tool_call_id="toolu_" + "0" * 20,
            tool_name=name,
            arguments=dict(arguments),
        ),
        context=ExecutionContext(
            principal=PrincipalContext(tenant_id="tenant_a", principal_id="user_owner"),
            envelope=AuthorizationEnvelope(),
            agent_run_id="run_" + "0" * 28,
            policy_identity="test",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=30,
    )


class TestExclusivity:
    """ADR-073 §5.1, and the reason every other test here is worth having."""

    def test_the_two_tool_sets_are_disjoint(self) -> None:
        # If this ever fails, a model has two "write a file" tools whose
        # descriptions differ only in a word it cannot verify. The failure that
        # follows does not raise: `project_write("draft.md", …)` succeeds and
        # puts a scratch file in somebody's repository root.
        assert set(CODE_TOOLS).isdisjoint(CODE_PROJECT_TOOLS)

    def test_the_sandbox_variants_stay_disjoint_apart_from_the_sandbox(self) -> None:
        # The one name both may carry is `sandbox_run`, which is neither set's
        # file language. Asserted rather than assumed: the two tuples are
        # written out by hand, and the way that breaks is somebody appending to
        # the wrong one.
        shared = set(CODE_TOOLS_WITH_SANDBOX) & set(CODE_PROJECT_TOOLS_WITH_SANDBOX)
        assert shared == {"sandbox_run"}

    def test_each_project_tool_is_named_in_the_project_set(self) -> None:
        # The registry and the tuple are written in different files, and a tool
        # registered but never offered is dead weight nobody notices.
        registered = {
            ProjectListTool(ProjectFileScope()).spec().name,
            ProjectReadTool(ProjectFileScope()).spec().name,
            ProjectWriteTool(ProjectFileScope()).spec().name,
            ProjectEditTool(ProjectFileScope()).spec().name,
        }
        assert registered == set(CODE_PROJECT_TOOLS)


class TestRefusalWithoutAScope:
    async def test_every_tool_refuses_when_no_directory_was_entered(
        self, scope: ProjectFileScope
    ) -> None:
        # Refused, not "open one". The only root this could pick is one nobody
        # registered, and writing a model's output into a directory the user
        # never chose is the worst thing this subsystem could do.
        for tool, call in (
            (ProjectListTool(scope), _invocation("project_list")),
            (ProjectReadTool(scope), _invocation("project_read", path="a.md")),
            (
                ProjectWriteTool(scope),
                _invocation("project_write", path="a.md", content="x"),
            ),
            (
                ProjectEditTool(scope),
                _invocation("project_edit", path="a.md", find="x", replace="y"),
            ),
        ):
            with pytest.raises(ProjectFilesUnavailableError):
                await tool.handle(call)

    async def test_nothing_is_written_when_the_scope_is_missing(
        self, project: Path, scope: ProjectFileScope
    ) -> None:
        before = sorted(item.name for item in project.iterdir())
        with pytest.raises(ProjectFilesUnavailableError):
            await ProjectWriteTool(scope).handle(
                _invocation("project_write", path="new.md", content="x")
            )
        assert sorted(item.name for item in project.iterdir()) == before


class TestInsideAScope:
    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    async def test_listing_marks_directories(self, entered: ProjectFileScope) -> None:
        result = await ProjectListTool(entered).handle(_invocation("project_list"))
        assert result.content is not None
        assert "src/" in result.content
        assert "README.md" in result.content

    async def test_reading_returns_the_text(self, entered: ProjectFileScope) -> None:
        result = await ProjectReadTool(entered).handle(
            _invocation("project_read", path="src/main.py")
        )
        assert result.content == "print('hi')\n"

    async def test_writing_creates_parent_directories(
        self, entered: ProjectFileScope, project: Path
    ) -> None:
        # The capability the flat workspace could not offer, reached through a
        # tool this time rather than through the store.
        result = await ProjectWriteTool(entered).handle(
            _invocation("project_write", path="docs/adr/0073.md", content="# 073\n")
        )
        assert result.error is None
        assert (project / "docs" / "adr" / "0073.md").read_text() == "# 073\n"

    async def test_editing_replaces_exactly_one_occurrence(
        self, entered: ProjectFileScope, project: Path
    ) -> None:
        result = await ProjectEditTool(entered).handle(
            _invocation("project_edit", path="src/main.py", find="hi", replace="bye")
        )
        assert result.error is None
        assert (project / "src" / "main.py").read_text() == "print('bye')\n"

    async def test_an_ambiguous_edit_is_refused_rather_than_guessed(
        self, entered: ProjectFileScope, project: Path
    ) -> None:
        (project / "twice.txt").write_text("a\na\n")
        result = await ProjectEditTool(entered).handle(
            _invocation("project_edit", path="twice.txt", find="a", replace="b")
        )
        # "the first one" is a guess dressed as a result: the model cannot know
        # which occurrence it changed.
        assert result.error is not None
        assert "exactly once" in result.error.message
        assert (project / "twice.txt").read_text() == "a\na\n"

    async def test_a_missing_snippet_is_refused(
        self, entered: ProjectFileScope
    ) -> None:
        result = await ProjectEditTool(entered).handle(
            _invocation("project_edit", path="src/main.py", find="nope", replace="x")
        )
        assert result.error is not None
        assert "0 times" in result.error.message


class TestTheToolsDoNotBypassTheSandbox:
    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    @pytest.mark.parametrize(
        "path", ["../../etc/passwd", "/etc/passwd", "a\x00b", "src/../../../x"]
    )
    async def test_a_path_leaving_the_project_is_refused_by_every_writer(
        self, entered: ProjectFileScope, path: str
    ) -> None:
        # Refused as a *result*, not as an exception: a tool that raises ends
        # the turn, and a model that gave a bad path should be told so and get
        # another step.
        for tool, call in (
            (ProjectReadTool(entered), _invocation("project_read", path=path)),
            (
                ProjectWriteTool(entered),
                _invocation("project_write", path=path, content="x"),
            ),
        ):
            result = await tool.handle(call)
            assert result.error is not None
            assert result.error.code in {"invalid_tool_input", "not_found"}

    async def test_a_symlink_out_of_the_project_is_refused(
        self, entered: ProjectFileScope, project: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE\n")
        (project / "link.txt").symlink_to(secret)
        result = await ProjectReadTool(entered).handle(
            _invocation("project_read", path="link.txt")
        )
        assert result.error is not None
        assert secret.read_text() == "PRIVATE\n"


async def test_a_binary_file_is_described_not_decoded(
    project: Path, scope: ProjectFileScope
) -> None:
    (project / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    store = FilesystemProjectFileStore(ProjectSandbox(project))
    with scope.using(store):
        result = await ProjectReadTool(scope).handle(
            _invocation("project_read", path="logo.png")
        )
    # Told, not handed. A PNG decoded with replacement is a string of U+FFFD
    # that a model reads as text and then edits, and the edit destroys the file.
    assert result.error is None
    assert result.content is not None
    assert "not a text file" in result.content


# --- helpers for the per-turn choice -----------------------------------------
#
# A stub rather than a real `ProjectService`: what is under test is which of
# four states resolves to a directory, and a real service would drag a store, an
# engine and a principal resolver in to answer a question none of them decide.


class _ProjectsStub:
    def __init__(self, root: Path | None) -> None:
        self._root = root

    async def open_files(self, principal: object, project_id: str) -> object:
        if self._root is None:
            raise NotFoundError(f"project {project_id!r} has no directory")
        return FilesystemProjectFileStore(ProjectSandbox(self._root))


def _projects_stub(root: Path | None) -> _ProjectsStub:
    return _ProjectsStub(root)


def _principal() -> PrincipalContext:
    return PrincipalContext(tenant_id="tenant_a", principal_id="user_owner")


def _service(
    *, projects: object, scope: ProjectFileScope | None = _UNSET
) -> CodeSessionService:
    """A service built with only the fields `_project_files_for` reads.

    `object.__new__` rather than the constructor: `CodeSessionService` needs a
    conversation store, an executor factory, a budget and four more things to be
    constructed, and none of them participate in this decision. Building them
    would make the test assert on the assembly instead of on the choice.
    """

    service = object.__new__(CodeSessionService)
    object.__setattr__(service, "projects", projects)
    object.__setattr__(
        service,
        "project_scope",
        ProjectFileScope() if scope is _UNSET else scope,
    )
    return service


class TestTheChoiceIsPerTurn:
    """ADR-073 §5.2, at the seam that actually makes the choice.

    `_project_files_for` is the whole decision: everything downstream reads its
    answer. Tested directly rather than through a full turn because a turn needs
    a model, and what is worth pinning here is which of the four states resolves
    to a directory -- not that a turn runs.
    """

    async def test_no_project_means_the_flat_workspace(self) -> None:
        service = _service(projects=None)
        assert (
            await service._project_files_for(principal=_principal(), project_id=None)
            is None
        )

    async def test_a_deployment_without_the_capability_means_the_flat_workspace(
        self, project: Path
    ) -> None:
        # `projects` present, `project_scope` absent. A build that cannot enter
        # the scope must not offer the tools, or the model gets a tool list its
        # own process will refuse every call from.
        service = _service(projects=_projects_stub(project), scope=None)
        assert (
            await service._project_files_for(principal=_principal(), project_id="prj_1")
            is None
        )

    async def test_a_project_without_a_directory_means_the_flat_workspace(
        self,
    ) -> None:
        service = _service(projects=_projects_stub(None))
        assert (
            await service._project_files_for(principal=_principal(), project_id="prj_1")
            is None
        )

    async def test_a_project_with_a_directory_means_the_directory(
        self, project: Path
    ) -> None:
        service = _service(projects=_projects_stub(project))
        store = await service._project_files_for(
            principal=_principal(), project_id="prj_1"
        )
        assert store is not None
        assert (await store.read("README.md")).text == "# alpha\n"
