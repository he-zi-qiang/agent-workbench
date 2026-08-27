"""The project-directory tools, and the exclusivity that makes them safe.

The most important test in this file is the shortest one: the two tool sets are
disjoint. Everything else here checks that a tool does what it says; that one
checks that a model is never in a position to have to choose between two tools
that both say "write a file" (ADR-073 §2).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_workbench.adapters.filesystem.project_files import (
    FilesystemProjectFileStore,
)
from agent_workbench.adapters.filesystem.sandbox import ProjectSandbox
from agent_workbench.adapters.tools import project_files
from agent_workbench.adapters.tools.project_files import (
    ProjectEditTool,
    ProjectFilesUnavailableError,
    ProjectGrepTool,
    ProjectListTool,
    ProjectReadTool,
    ProjectRunTool,
    ProjectWriteTool,
)
from agent_workbench.application.code_session import (
    CODE_PROJECT_TOOLS,
    CODE_PROJECT_TOOLS_WITH_RUN,
    CODE_TOOLS,
    CODE_TOOLS_WITH_SANDBOX,
    CodeSessionService,
    code_risk_ceiling,
    read_only,
)
from agent_workbench.application.file_read_receipts import ReadReceipts
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
    risk_within,
)
from agent_workbench.domain.tools import ToolCall, ToolSpec
from agent_workbench.domain.workspace import MAX_INLINE_READ_CHARS
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.project_files import ProjectFileStore
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


@pytest.fixture
def receipts() -> Iterator[ReadReceipts]:
    """An entered, empty ledger, the way one turn gets one (ADR-0078).

    Entered rather than merely constructed: every method on `ReadReceipts`
    refuses outside a turn, on purpose, so a test that forgot this would fail
    on the refusal rather than silently exercise a gate that checks nothing.
    """

    ledger = ReadReceipts()
    with ledger.using():
        yield ledger


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

    def test_no_project_tuple_offers_a_tool_bound_to_the_flat_workspace(self) -> None:
        # `sandbox_run` used to be the one name both sides could carry, on the
        # reasoning that it is neither set's file language. It is not a file
        # language question: the tool reads its session out of `WorkspaceScope`
        # and `CodeSessionService.run` enters exactly one scope, so on a project
        # turn every call raised `SandboxUnavailableError` and the model was
        # handed `unhandled SandboxUnavailableError`. Under `demo-local`, where
        # every session has a project, that was every call.
        flat = set(CODE_TOOLS_WITH_SANDBOX)
        for offered in (CODE_PROJECT_TOOLS, CODE_PROJECT_TOOLS_WITH_RUN):
            assert flat.isdisjoint(offered)

    def test_each_project_tool_is_named_in_the_project_set(self) -> None:
        # The registry and the tuple are written in different files, and a tool
        # registered but never offered is dead weight nobody notices.
        registered = {
            ProjectListTool(ProjectFileScope()).spec().name,
            ProjectReadTool(ProjectFileScope(), ReadReceipts()).spec().name,
            ProjectWriteTool(ProjectFileScope(), ReadReceipts()).spec().name,
            ProjectEditTool(ProjectFileScope(), ReadReceipts()).spec().name,
            ProjectGrepTool(ProjectFileScope()).spec().name,
        }
        assert registered == set(CODE_PROJECT_TOOLS)

    def test_the_run_tool_is_named_only_in_the_tuples_that_opt_into_it(self) -> None:
        # Its own assertion rather than a sixth entry above, because it is not
        # in the base set and must not be: `project_run` is offered only where
        # `policy.shell_tools_enabled` says the machine may be driven at all
        # (ADR-077). The rule above still applies to it -- registered but named
        # nowhere is dead weight -- it just applies to a different tuple.
        name = (
            ProjectRunTool(ProjectFileScope(), ReadReceipts(), environment={})
            .spec()
            .name
        )
        assert name not in CODE_PROJECT_TOOLS
        assert name in CODE_PROJECT_TOOLS_WITH_RUN

    def test_no_flat_workspace_tuple_ever_carries_the_run_tool(self) -> None:
        # A command needs a directory to run in, and a flat-workspace turn has
        # none. The absence is the answer rather than an omission, so it is
        # asserted rather than left to whoever edits these tuples next.
        name = (
            ProjectRunTool(ProjectFileScope(), ReadReceipts(), environment={})
            .spec()
            .name
        )
        assert name not in CODE_TOOLS
        assert name not in CODE_TOOLS_WITH_SANDBOX

    @staticmethod
    def _specs() -> dict[str, ToolSpec]:
        return {
            tool.spec().name: tool.spec()
            for tool in (
                ProjectListTool(ProjectFileScope()),
                ProjectReadTool(ProjectFileScope(), ReadReceipts()),
                ProjectWriteTool(ProjectFileScope(), ReadReceipts()),
                ProjectEditTool(ProjectFileScope(), ReadReceipts()),
                ProjectGrepTool(ProjectFileScope()),
                ProjectRunTool(ProjectFileScope(), ReadReceipts(), environment={}),
            )
        }

    def test_the_ceiling_admits_exactly_what_the_turn_was_given(self) -> None:
        # Two switches would be two ways to describe one decision, and the
        # interesting bug is the pair disagreeing: a turn granted a tool its own
        # envelope denies burns a whole turn on `outside_submitted_envelope`.
        risks = {name: spec.risk for name, spec in self._specs().items()}

        assert code_risk_ceiling(CODE_PROJECT_TOOLS, risks=risks) == "write"
        assert (
            code_risk_ceiling(CODE_PROJECT_TOOLS_WITH_RUN, risks=risks) == "destructive"
        )
        # ADR-0079's fourth value, and nobody wrote a branch for it: a turn
        # narrowed to the reading tools derives a `read` ceiling because that
        # is what those tools say about themselves.
        assert (
            code_risk_ceiling(
                read_only(CODE_PROJECT_TOOLS_WITH_RUN, risks=risks), risks=risks
            )
            == "read"
        )

    def test_an_offered_tool_with_no_spec_raises_rather_than_defaulting(self) -> None:
        # Both available defaults are wrong in a way nothing downstream reports.
        # `read` builds an envelope denying a tool the turn was given;
        # `destructive` raises the ceiling of every turn containing a typo.
        with pytest.raises(ValueError, match="no spec"):
            code_risk_ceiling(("project_teleport",), risks={})

    def test_every_offered_tool_is_within_the_ceiling_it_derives(self) -> None:
        # The property the derivation exists for, checked against the specs
        # rather than against the code that produced it -- otherwise this would
        # be re-asserting the same comprehension.
        specs = self._specs()
        risks = {name: spec.risk for name, spec in specs.items()}
        for names in (
            CODE_PROJECT_TOOLS,
            CODE_PROJECT_TOOLS_WITH_RUN,
            read_only(CODE_PROJECT_TOOLS_WITH_RUN, risks=risks),
        ):
            ceiling = code_risk_ceiling(names, risks=risks)
            for name in names:
                assert risk_within(specs[name].risk, ceiling), (
                    f"{name} is offered by a turn whose ceiling is {ceiling}"
                )

    def test_plan_mode_only_ever_narrows(self) -> None:
        """ADR-0079 invariant 1, checkable by reading the two lists.

        `read_only` preserves order, so a plan turn's tool list is a
        subsequence of the act turn's -- which is what makes "plan can never
        add a tool" a property somebody can see rather than one they have to
        trust the filter for.
        """

        risks = {name: spec.risk for name, spec in self._specs().items()}
        for offered in (CODE_PROJECT_TOOLS, CODE_PROJECT_TOOLS_WITH_RUN):
            planned = read_only(offered, risks=risks)

            assert set(planned) <= set(offered)
            assert list(planned) == [n for n in offered if n in set(planned)]
            assert all(risks[name] == "read" for name in planned)
            # And it is not empty: a plan turn that cannot read anything is a
            # turn that can only guess, which is worse than no plan mode.
            assert planned


class TestRefusalWithoutAScope:
    async def test_every_tool_refuses_when_no_directory_was_entered(
        self,
        scope: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        # Refused, not "open one". The only root this could pick is one nobody
        # registered, and writing a model's output into a directory the user
        # never chose is the worst thing this subsystem could do.
        for tool, call in (
            (ProjectListTool(scope), _invocation("project_list")),
            (
                ProjectReadTool(scope, receipts),
                _invocation("project_read", path="a.md"),
            ),
            (
                ProjectWriteTool(scope, receipts),
                _invocation("project_write", path="a.md", content="x"),
            ),
            (
                ProjectEditTool(scope, receipts),
                _invocation("project_edit", path="a.md", find="x", replace="y"),
            ),
            (ProjectGrepTool(scope), _invocation("project_grep", pattern="x")),
            (
                ProjectRunTool(scope, receipts, environment={}),
                _invocation("project_run", command="ls"),
            ),
        ):
            with pytest.raises(ProjectFilesUnavailableError):
                await tool.handle(call)

    async def test_nothing_is_written_when_the_scope_is_missing(
        self,
        project: Path,
        scope: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        before = sorted(item.name for item in project.iterdir())
        with pytest.raises(ProjectFilesUnavailableError):
            await ProjectWriteTool(scope, receipts).handle(
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

    async def test_reading_returns_the_text(
        self, entered: ProjectFileScope, receipts: ReadReceipts
    ) -> None:
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="src/main.py")
        )
        assert result.content == "print('hi')\n"

    async def test_writing_creates_parent_directories(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # The capability the flat workspace could not offer, reached through a
        # tool this time rather than through the store.
        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="docs/adr/0073.md", content="# 073\n")
        )
        assert result.error is None
        assert (project / "docs" / "adr" / "0073.md").read_text() == "# 073\n"

    async def test_editing_replaces_exactly_one_occurrence(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        result = await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="src/main.py", find="hi", replace="bye")
        )
        assert result.error is None
        assert (project / "src" / "main.py").read_text() == "print('bye')\n"

    async def test_an_ambiguous_edit_is_refused_rather_than_guessed(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        (project / "twice.txt").write_text("a\na\n")
        result = await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="twice.txt", find="a", replace="b")
        )
        # "the first one" is a guess dressed as a result: the model cannot know
        # which occurrence it changed.
        assert result.error is not None
        assert "exactly once" in result.error.message
        assert (project / "twice.txt").read_text() == "a\na\n"

    async def test_a_missing_snippet_is_refused(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        result = await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="src/main.py", find="nope", replace="x")
        )
        assert result.error is not None
        assert "0 times" in result.error.message


class _AppearsWhileYouLook:
    """A store where a file arrives between the existence check and the write.

    The create path's own window: `project_write` asks `exists`, is told no,
    and writes. Delegating everything except `exists` keeps the rest real.
    """

    def __init__(self, inner: ProjectFileStore, meanwhile: Callable[[], None]) -> None:
        self._inner = inner
        self._meanwhile = meanwhile

    async def exists(self, path: str) -> bool:
        answer = await self._inner.exists(path)
        self._meanwhile()
        return answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SavesWhileYouRead:
    """A store that lets somebody else save the file between read and write.

    The one window `project_edit` cannot close on its own: it reads, decides,
    and writes, and those are separate awaits. Delegating everything except
    `read` keeps the rest of the store real, so what the test exercises is the
    precondition rather than a simulation of one.
    """

    def __init__(self, inner: ProjectFileStore, meanwhile: Callable[[], None]) -> None:
        self._inner = inner
        self._meanwhile = meanwhile

    async def read(self, path: str):
        content = await self._inner.read(path)
        self._meanwhile()
        return content

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestAFileYouHaveNotReadIsNotYoursToOverwrite:
    """ADR-0078: the gate discipline 1 has always asked for and nothing kept.

    `project_write` replaces a whole file with the text it was handed. Before
    this it never asked whether the model had seen the file it was replacing,
    so a user's unsaved-to-the-model edit was overwritten with an `ok`, a step
    line reading "写入项目文件", and a transcript that looked like a good turn.
    """

    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    async def test_replacing_an_unread_file_is_refused_and_says_to_read_it(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# replaced\n")
        )

        assert result.error is not None
        assert result.error.code == "invalid_tool_input"
        # The message has to name the read. `invalid_tool_input` is normally
        # terminal for a route -- discipline 3 tells the model that retrying a
        # refused call cannot succeed -- and this is the refusal where trying
        # again, after one more call, is exactly right.
        assert "have not read it" in result.error.message
        assert "Read it first" in result.error.message
        # And the file is untouched, which is the whole point.
        assert (project / "README.md").read_text() == "# alpha\n"

    async def test_creating_a_new_file_needs_no_receipt(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # Only a replacement is gated. Requiring a read of a path that is not
        # there would refuse the most ordinary thing a coding agent does, with
        # a sentence naming an action nobody can perform.
        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="NEW.md", content="# new\n")
        )

        assert result.error is None
        assert (project / "NEW.md").read_text() == "# new\n"

    async def test_a_whole_file_read_opens_the_gate(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="README.md")
        )

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# replaced\n")
        )

        assert result.error is None
        assert (project / "README.md").read_text() == "# replaced\n"

    async def test_a_windowed_read_does_not_open_it(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The case a receipt table gets wrong if it records every read.

        A window is a successful read that did not hand over the file. Counting
        it would licence an overwrite of bytes the model provably never saw --
        discipline 1's exact failure mode, now carrying a green light.
        """

        (project / "long.txt").write_text("".join(f"line {n}\n" for n in range(50)))
        windowed = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="long.txt", offset=1, limit=5)
        )
        assert windowed.error is None

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="long.txt", content="clobbered\n")
        )

        assert result.error is not None
        assert "only part of long.txt" in result.error.message
        # It names both ways forward, because "read it first" is not the only
        # one and is the expensive one on a long file.
        assert "project_edit" in result.error.message
        assert (project / "long.txt").read_text().startswith("line 0\n")

    async def test_a_binary_read_does_not_open_it(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # A non-text read succeeds and shows nothing ("Its contents are not
        # shown"). The model has seen the size and the verdict, not the file.
        (project / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        shown = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="logo.png")
        )
        assert shown.error is None

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="logo.png", content="text now\n")
        )

        assert result.error is not None
        assert "have not read it" in result.error.message

    async def test_a_file_that_moved_after_the_read_is_refused(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The user's editor, mid-turn. The failure this whole thing is for."""

        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="README.md")
        )
        (project / "README.md").write_text("# the user's own edit, just now\n")

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# the model\n")
        )

        assert result.error is not None
        assert "changed after you read it" in result.error.message
        assert "Something outside this turn changed it" in result.error.message
        assert (
            project / "README.md"
        ).read_text() == "# the user's own edit, just now\n"

    async def test_when_this_turn_ran_a_command_the_refusal_says_so(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """Two sentences, because they call for different next moves.

        A formatter this turn started with `project_run` moving an mtime is the
        model's own doing and wants a re-read. An edit from outside the turn is
        somebody else at work on the same file, and a model told *that* should
        say so in its report rather than quietly writing again.
        """

        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="README.md")
        )
        receipts.note_command_ran()
        (project / "README.md").write_text("# reformatted\n")

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# the model\n")
        )

        assert result.error is not None
        assert "A command you ran in this turn may have done it" in result.error.message
        assert "outside this turn" not in result.error.message

    async def test_writing_refreshes_the_receipt_so_the_next_write_passes(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """Without this the model's own second write is refused by its own gate.

        The receipt from the read describes the file as it was read, and the
        write it just made moved both the size and the mtime -- so the values
        come from the entry `ProjectFileStore.write` returns, which is what
        that return value is for.
        """

        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="README.md")
        )
        first = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# one\n")
        )
        assert first.error is None

        second = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# two\n")
        )

        assert second.error is None
        assert (project / "README.md").read_text() == "# two\n"

    async def test_a_narrow_read_does_not_undo_a_whole_one(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """Coverage is carried forward on the read path too, not just on edit.

        Re-reading a region before rewriting it is the most ordinary check a
        coding agent makes. Letting the narrower read overwrite the wider one's
        receipt refused a model that was strictly better informed than the one
        allowed through a call earlier, and the sentence it got was wrong about
        what it had seen.
        """

        (project / "k.py").write_text("".join(f"line {n}\n" for n in range(40)))
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="k.py")
        )
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="k.py", offset=10, limit=6)
        )

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="k.py", content="ok\n")
        )

        assert result.error is None

    async def test_a_narrow_read_of_a_file_that_moved_does_not_keep_coverage(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # The guard on the carry-forward. If size or mtime differ, the earlier
        # read describes a file that is gone, and this window's coverage is the
        # true one -- carrying the old flag would be the laundering bug again,
        # reached from the read side.
        (project / "m.py").write_text("aaa\n")
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="m.py")
        )
        (project / "m.py").write_text("".join(f"row {n}\n" for n in range(40)))
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="m.py", offset=2, limit=3)
        )

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="m.py", content="x\n")
        )

        assert result.error is not None
        assert "only part of m.py" in result.error.message

    async def test_the_windowed_refusal_names_a_move_that_can_work(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The first version named the one move that never works.

        Coverage comes from a window that starts at line 1 and reaches the end,
        so "read the rest -- the read told you which offset continues" replaces
        a head receipt with a tail receipt and returns the identical refusal.
        Verified below on a file whose every line has been handed over across
        two reads: still refused, correctly, and now the message names the two
        moves that do work.
        """

        long_enough = "".join(f"line {n} {'z' * 60}\n" for n in range(1200))
        assert len(long_enough) > MAX_INLINE_READ_CHARS
        (project / "big.py").write_text(long_enough)

        first = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="big.py")
        )
        assert first.content is not None
        rest = first.content.split(" to continue.")[0].split("pass offset=")[-1]
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="big.py", offset=int(rest))
        )

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="big.py", content="gone\n")
        )

        assert result.error is not None
        assert "which offset continues" not in result.error.message
        assert "no offset and no limit" in result.error.message
        assert "project_edit" in result.error.message
        assert (project / "big.py").read_text() == long_enough

    async def test_a_file_that_appears_between_the_check_and_the_write_is_refused(
        self,
        scope: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The create path had the window `if_unchanged` exists to close.

        `project_write` asks `store.exists` and then writes: two separate hops
        onto the executor, and the user's editor saves into windows like that.
        The answer "it is not there" licensed an unconditional write, so a file
        that appeared in between was replaced having never been read -- past
        both ADR-0078 invariant 1 and ADR-072's new invariant 6.
        """

        inner = FilesystemProjectFileStore(ProjectSandbox(project))
        racing = cast(
            ProjectFileStore,
            _AppearsWhileYouLook(
                inner,
                lambda: (project / "notes.md").write_text("# the user's own\n"),
            ),
        )
        with scope.using(racing):
            result = await ProjectWriteTool(scope, receipts).handle(
                _invocation("project_write", path="notes.md", content="# model\n")
            )

        assert result.error is not None
        assert "already exists" in result.error.message
        # The sentence asks for a read, not a re-read: nothing in this turn has
        # ever seen this file, so "read it again" would name the wrong move.
        assert "Read it before writing it" in result.error.message
        assert (project / "notes.md").read_text() == "# the user's own\n"

    async def test_an_edit_needs_no_receipt_because_it_reads_the_file_itself(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # `project_edit` reads one statement before it writes and refuses
        # unless the snippet appears exactly once, so it can never be working
        # from a stale copy. Gating it on a receipt too would refuse the safe
        # tool and leave the model reaching for the dangerous one.
        result = await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="README.md", find="alpha", replace="beta")
        )

        assert result.error is None
        assert (project / "README.md").read_text() == "# beta\n"

    async def test_an_edit_cannot_launder_a_file_into_being_read(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The two-call bypass that a refresh-on-edit opens, if the refresh mints.

        `project_edit` is handed a snippet: the *store* reads the file, the
        model does not. An edit that recorded a whole-file receipt therefore
        let a model overwrite a file it had never seen in two calls -- edit one
        unique string, then `project_write` over all of it, with the gate's
        approval. Measured on a 30 KB file before the fix.
        """

        big = "def f():\n" + "".join(f"    line_{n}()\n" for n in range(2000))
        (project / "big.py").write_text(big + "MARKER\n")

        edited = await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="big.py", find="MARKER", replace="MARKED")
        )
        assert edited.error is None
        assert receipts.seen("big.py") is None

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="big.py", content="# gone\n")
        )

        assert result.error is not None
        assert "have not read it" in result.error.message
        assert (project / "big.py").read_text().startswith("def f():\n")

    async def test_an_edit_keeps_a_receipt_the_model_had_already_earned(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """Carried forward, not manufactured -- and not thrown away either.

        A model that read the whole file and then edited it still knows the
        whole file: what it holds is the text it read plus the replacement it
        wrote. Dropping the receipt here would refuse its own next write and
        cost a re-read for nothing.
        """

        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="README.md")
        )
        await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="README.md", find="alpha", replace="beta")
        )

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="README.md", content="# whole\n")
        )

        assert result.error is None
        assert (project / "README.md").read_text() == "# whole\n"

    async def test_an_edit_after_a_windowed_read_does_not_promote_it(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # The same laundering one step along: a window is not whole-file
        # coverage before an edit and must not become it after one.
        (project / "long.txt").write_text("".join(f"line {n}\n" for n in range(50)))
        await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="long.txt", offset=1, limit=5)
        )
        await ProjectEditTool(entered, receipts).handle(
            _invocation("project_edit", path="long.txt", find="line 3", replace="X")
        )

        carried = receipts.seen("long.txt")
        assert carried is not None
        assert not carried.covers_whole_file

        result = await ProjectWriteTool(entered, receipts).handle(
            _invocation("project_write", path="long.txt", content="clobbered\n")
        )

        assert result.error is not None
        assert "only part of long.txt" in result.error.message

    async def test_an_edit_will_not_write_over_a_change_it_did_not_see(
        self,
        scope: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        """The window inside `project_edit` itself.

        Its read and its write are two separate hops onto the executor, and the
        user's editor saves into windows like that. Driven here by a store that
        lets the save happen at exactly that moment -- a wrapper rather than a
        monkeypatch, because `FilesystemProjectFileStore` has `__slots__` and
        an attribute on it cannot be replaced.

        Without `if_unchanged` the edit writes its replacement over the user's
        text and reports success, which is the same silent loss as the unread
        overwrite one class up, reached by the tool that looks safe.
        """

        inner = FilesystemProjectFileStore(ProjectSandbox(project))
        racing = cast(
            ProjectFileStore,
            _SavesWhileYouRead(
                inner,
                lambda: (project / "README.md").write_text("# saved by the user\n"),
            ),
        )
        with scope.using(racing):
            result = await ProjectEditTool(scope, receipts).handle(
                _invocation(
                    "project_edit", path="README.md", find="alpha", replace="beta"
                )
            )

        assert result.error is not None
        assert "changed after you read it" in result.error.message
        assert (project / "README.md").read_text() == "# saved by the user\n"


class TestReadingAFileTheModelCannotHoldAtOnce:
    """What a read says when the file does not fit, and when there is nothing.

    Both used to be answered in a way the model could not act on: an empty file
    came back as an empty tool message, and a long one came back as a
    *success* carrying its first 48,000 characters with no argument that could
    ever reach the rest.
    """

    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    async def test_an_empty_file_says_so_instead_of_returning_nothing(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # An empty tool message reads as a call that was ignored, so the model
        # sends the same one again and `MAX_IDENTICAL_CALLS` ends the turn on
        # the third. A zero-byte `__init__.py` is not an unusual thing to open.
        (project / "src" / "__init__.py").write_text("")
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="src/__init__.py")
        )
        assert result.error is None
        assert result.content == "src/__init__.py is empty."

    async def test_the_tail_of_a_long_file_is_reachable(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # The defect this closes: the head came back and nothing could ask for
        # the rest. On a project turn there is no second route to it either --
        # `sandbox_run` cannot see the directory at all.
        (project / "long.txt").write_text("".join(f"line {n}\n" for n in range(9_000)))
        head = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="long.txt")
        )
        assert head.content is not None
        assert "pass offset=" in head.content
        resume = int(head.content.split("pass offset=")[1].split(" ")[0].rstrip("."))

        tail = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="long.txt", offset=resume)
        )
        assert tail.content is not None
        assert "line 8999" in tail.content

    async def test_a_window_says_which_lines_it_gave(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        (project / "long.txt").write_text("".join(f"{n}\n" for n in range(1, 51)))
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="long.txt", offset=10, limit=5)
        )
        assert result.content is not None
        assert result.content.startswith("long.txt: lines 10-14 of 50;")
        assert result.content.endswith("10\n11\n12\n13\n14\n")

    async def test_an_offset_past_the_end_is_a_refusal_not_the_last_line(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="src/main.py", offset=900)
        )
        assert result.error is not None
        assert "past the end" in result.error.message

    async def test_a_file_too_large_to_read_is_not_offered_an_offset(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # `MAX_READ_BYTES` is a refusal, not a truncation: there is no window
        # of this file that any offset reaches. Telling the model to "pass
        # offset" here would buy a sequence of calls that all refuse the same
        # way, against a bounded tool allowance.
        (project / "huge.bin").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="huge.bin")
        )
        assert result.error is not None
        assert result.error.code == "output_too_large"
        assert "offset" not in result.error.message


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
        self,
        entered: ProjectFileScope,
        path: str,
        receipts: ReadReceipts,
    ) -> None:
        # Refused as a *result*, not as an exception: a tool that raises ends
        # the turn, and a model that gave a bad path should be told so and get
        # another step.
        for tool, call in (
            (
                ProjectReadTool(entered, receipts),
                _invocation("project_read", path=path),
            ),
            (
                ProjectWriteTool(entered, receipts),
                _invocation("project_write", path=path, content="x"),
            ),
        ):
            result = await tool.handle(call)
            assert result.error is not None
            assert result.error.code in {"invalid_tool_input", "not_found"}

    async def test_a_symlink_out_of_the_project_is_refused(
        self,
        entered: ProjectFileScope,
        project: Path,
        tmp_path: Path,
        receipts: ReadReceipts,
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE\n")
        (project / "link.txt").symlink_to(secret)
        result = await ProjectReadTool(entered, receipts).handle(
            _invocation("project_read", path="link.txt")
        )
        assert result.error is not None
        assert secret.read_text() == "PRIVATE\n"


async def test_a_binary_file_is_described_not_decoded(
    project: Path,
    scope: ProjectFileScope,
    receipts: ReadReceipts,
) -> None:
    (project / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    store = FilesystemProjectFileStore(ProjectSandbox(project))
    with scope.using(store):
        result = await ProjectReadTool(scope, receipts).handle(
            _invocation("project_read", path="logo.png")
        )
    # Told, not handed. A PNG decoded with replacement is a string of U+FFFD
    # that a model reads as text and then edits, and the edit destroys the file.
    assert result.error is None
    assert result.content is not None
    assert "not a text file" in result.content


class TestSearchingTheTree:
    """`project_grep`, and the sentence it exists to be able to say.

    The matching is `grep_workspace`'s and is tested against a manifest
    elsewhere. What is under test here is everything a *real tree* adds: four
    independent ways for the answer to come back incomplete, and whether the
    reply admits to each one. A grep that quietly skipped a file would not fail
    a test that only asked "does it find what is there" -- it finds what is
    there. It fails the model later, once, in a way nobody can see.
    """

    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    async def _grep(self, scope: ProjectFileScope, **arguments: object) -> str:
        result = await ProjectGrepTool(scope).handle(
            _invocation("project_grep", **arguments)
        )
        assert result.error is None, result.error
        assert result.content is not None
        return result.content

    async def test_a_match_is_reported_with_its_path_and_line(
        self, entered: ProjectFileScope
    ) -> None:
        content = await self._grep(entered, pattern="print")
        assert "src/main.py:1: print('hi')" in content

    async def test_path_narrows_the_search_to_one_subtree(
        self, entered: ProjectFileScope
    ) -> None:
        content = await self._grep(entered, pattern="alpha", path="src")
        # README.md holds "# alpha" and sits at the root, so finding nothing is
        # the whole point: `path` has to actually bound the walk rather than
        # filter its results after the fact.
        assert content.startswith("No matches.")

    async def test_a_glob_crosses_directories(self, entered: ProjectFileScope) -> None:
        # `fnmatch`'s `*` spans `/`, which is what makes '*.py' useful on a tree
        # and is why the tool's description says so. A model that read it as
        # "top level only" would narrow searches it meant to widen.
        content = await self._grep(entered, pattern="print", name_glob="*.py")
        assert "src/main.py:1" in content

    async def test_a_glob_that_matches_nothing_says_so_not_no_matches(
        self, entered: ProjectFileScope
    ) -> None:
        # Distinguished on purpose. "No matches" is a statement about the
        # pattern; this is a statement about the glob, and a model told the
        # former would go looking for a different pattern.
        content = await self._grep(entered, pattern="print", name_glob="*.rs")
        assert "No files under" in content
        assert "*.rs" in content

    async def test_a_file_that_is_not_utf8_is_named_rather_than_skipped(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        (project / "logo.png").write_bytes(b"\xff\xfe\x00\x01binary")
        content = await self._grep(entered, pattern="binary")
        assert content.startswith("No matches.")
        assert "not text, so not searched: logo.png" in content

    async def test_a_nul_byte_in_valid_utf8_is_named_rather_than_quoted_back(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        # A NUL is valid UTF-8, so the store decodes it and reports `is_text`.
        # Quoting the matched line back would put U+0000 into the model prompt,
        # the prompt into a `ModelStarted` event, and PostgreSQL would refuse
        # the write -- killing a run whose only mistake was searching a
        # directory that happens to contain a compiled catalogue.
        # Valid UTF-8 -- that is the whole point. `is_text` comes back true and
        # the store hands over a decoded string with U+0000 still in it.
        (project / "messages.mo").write_bytes(b"alpha\x00beta\n")
        content = await self._grep(entered, pattern="alpha")
        assert "\x00" not in content
        assert "not text, so not searched: messages.mo" in content

    async def test_a_file_over_the_read_ceiling_is_named_as_too_large(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        (project / "huge.txt").write_text("needle\n" + "x" * (2 * 1024 * 1024))
        content = await self._grep(entered, pattern="needle")
        assert content.startswith("No matches.")
        assert "too large to search: huge.txt" in content

    async def test_the_read_budget_names_every_file_it_did_not_reach(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        # Five files of 1.9 MB against an 8 MiB budget: four are read, the fifth
        # is not. One long line each so the match pass stays trivial -- what is
        # under test is the read ceiling, not the regex engine's own clock.
        for name in ("a", "b", "c", "d", "e"):
            (project / f"{name}.txt").write_text("x" * 1_900_000 + "\n")
        content = await self._grep(entered, pattern="needle")
        assert content.startswith("No matches.")
        assert "not searched: e.txt" in content
        # And the ones it did reach are not named -- a caveat that listed every
        # file would be as useless as one that listed none.
        assert "a.txt" not in content

    async def test_the_files_read_do_not_depend_on_walk_order(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        # Same setup, twice. `walk` returns whatever `os.walk` produced, so
        # without the sort in `_read_corpus` the two runs could name different
        # files as unsearched and a model comparing them would be reading a
        # difference that is not there.
        for name in ("a", "b", "c", "d", "e"):
            (project / f"{name}.txt").write_text("x" * 1_900_000 + "\n")
        first = await self._grep(entered, pattern="needle")
        second = await self._grep(entered, pattern="needle")
        assert first == second

    async def test_a_negative_result_carries_every_caveat_at_once(
        self, project: Path, entered: ProjectFileScope
    ) -> None:
        # The test this class exists for. Three independent reasons the answer
        # is not exhaustive, all true at the same time, all of them said. A
        # reply that mentioned only the first would still read as a clean miss.
        (project / "logo.png").write_bytes(b"\xff\xfe\x00binary")
        (project / "huge.txt").write_text("x" * (2 * 1024 * 1024 + 1))
        content = await self._grep(entered, pattern="nowhere")
        assert content.startswith("No matches.")
        assert "not text, so not searched: logo.png" in content
        assert "too large to search: huge.txt" in content

    async def test_an_invalid_pattern_is_refused_as_bad_input(
        self, entered: ProjectFileScope
    ) -> None:
        result = await ProjectGrepTool(entered).handle(
            _invocation("project_grep", pattern="(unclosed")
        )
        assert result.error is not None
        assert result.error.code == "invalid_tool_input"

    async def test_a_scan_that_runs_long_is_stopped_and_not_retryable(
        self, entered: ProjectFileScope
    ) -> None:
        # The pattern is model-authored, so a catastrophically backtracking one
        # must not be able to hold the turn. Not retryable because the same
        # pattern over the same tree will do it again, and retrying is what a
        # model does with a transient error.
        ticks = iter((0.0, 0.0, 100.0))
        result = await ProjectGrepTool(
            entered, monotonic=lambda: next(ticks, 100.0)
        ).handle(_invocation("project_grep", pattern="print"))
        assert result.error is not None
        assert result.error.code == "tool_timeout"
        assert result.error.retryable is False

    def test_the_tool_is_a_read_and_therefore_safe(self) -> None:
        # `ToolSpec` enforces read⇒safe, so this is not re-checking pydantic:
        # it pins that the tool stayed a *read*. A search that ever acquired a
        # side effect would have to pass the Code envelope's `write` ceiling,
        # and the failure would surface as a denial, not as a diff.
        spec = ProjectGrepTool(ProjectFileScope()).spec()
        assert spec.risk == "read"
        assert spec.idempotency == "safe"
        assert spec.permission_scopes == ()


#: Enough environment for `/bin/sh` to find the coreutils these tests call.
#: Deliberately not `os.environ`: what the tool does with the environment it is
#: handed is one of the things under test, and a test that passed the real one
#: would be asserting against whatever the developer happened to export.
_MINIMAL_ENV = {"PATH": "/usr/bin:/bin"}


class TestRunningACommand:
    """`project_run`, which is the one tool here with no undo.

    Every test in this class is about a boundary rather than about the happy
    path, because the happy path is two lines of `asyncio` and the boundaries
    are where a tool that runs commands on somebody's machine goes wrong: what
    it inherits, what it leaves behind, and whether it stops.
    """

    @pytest.fixture
    def entered(self, project: Path, scope: ProjectFileScope):
        store = FilesystemProjectFileStore(ProjectSandbox(project))
        with scope.using(store):
            yield scope

    def _tool(
        self,
        scope: ProjectFileScope,
        receipts: ReadReceipts,
        **kwargs: object,
    ) -> ProjectRunTool:
        return ProjectRunTool(
            scope,
            receipts,
            environment=cast(
                Mapping[str, str], kwargs.pop("environment", _MINIMAL_ENV)
            ),
            **cast(Any, kwargs),
        )

    async def test_it_reports_the_exit_code_and_the_output(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        result = await self._tool(entered, receipts).handle(
            _invocation("project_run", command="echo hello")
        )
        assert result.error is None
        assert result.content is not None
        assert "exit code: 0" in result.content
        assert "hello" in result.content

    async def test_a_failing_command_is_a_result_not_an_error(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        # The traceback, the failing assertion, the compiler's line number --
        # those are the payload. A `ToolResult.failed` would hand the model an
        # error code where the answer it asked for is.
        result = await self._tool(entered, receipts).handle(
            _invocation("project_run", command="echo nope >&2; exit 3")
        )
        assert result.error is None
        assert result.content is not None
        assert "exit code: 3" in result.content
        assert "nope" in result.content

    async def test_both_streams_arrive_in_the_order_they_were_written(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        # One stream, because the thing being read is a terminal session: a test
        # runner's failing assertion and the line naming the test it belongs to
        # go to different channels, and separated they arrive as two lists
        # nobody can re-interleave.
        result = await self._tool(entered, receipts).handle(
            _invocation("project_run", command="echo one; echo two >&2; echo three")
        )
        assert result.content is not None
        body = result.content
        assert body.index("one") < body.index("two") < body.index("three")

    async def test_it_runs_in_the_project_directory(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        result = await self._tool(entered, receipts).handle(
            _invocation("project_run", command="ls")
        )
        assert result.content is not None
        assert "README.md" in result.content
        assert "src" in result.content

    async def test_it_passes_the_environment_it_was_given_and_no_more(
        self, entered: ProjectFileScope, receipts: ReadReceipts
    ) -> None:
        result = await self._tool(
            entered, receipts, environment={**_MINIMAL_ENV, "MARKER": "present"}
        ).handle(_invocation("project_run", command="echo $MARKER-$UNSET_ONE"))
        assert result.content is not None
        assert "present-" in result.content

    async def test_a_command_that_reads_stdin_does_not_hang(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        # stdin is /dev/null, so `cat` gets EOF immediately. Without that it
        # would wait on a human who is not there, look exactly like a slow
        # command, and be killed by the clock with nothing explaining why.
        result = await self._tool(entered, receipts, timeout_seconds=5.0).handle(
            _invocation("project_run", command="cat")
        )
        assert result.error is None
        assert result.content is not None
        assert "exit code: 0" in result.content

    async def test_a_command_that_runs_long_is_killed_and_said_to_be(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        result = await self._tool(entered, receipts, timeout_seconds=0.5).handle(
            _invocation("project_run", command="sleep 30")
        )
        assert result.error is not None
        assert result.error.code == "tool_timeout"
        assert result.error.retryable is False

    async def test_a_killed_command_still_says_what_it_had_printed(
        self,
        entered: ProjectFileScope,
        receipts: ReadReceipts,
    ) -> None:
        # Three failing tests and then a hang is a different problem from a
        # hang, and only one of the two answers tells the model which it is
        # looking at. The buffer therefore belongs to the caller of `_capture`
        # -- a version that built it locally and returned it would lose every
        # byte when the timeout cancelled the read.
        result = await self._tool(entered, receipts, timeout_seconds=1.0).handle(
            _invocation("project_run", command="echo FAILED::test_one; sleep 30")
        )
        assert result.error is not None
        assert result.error.code == "tool_timeout"
        assert "FAILED::test_one" in result.error.message

    async def test_nothing_the_command_started_outlives_it(
        self,
        entered: ProjectFileScope,
        project: Path,
        receipts: ReadReceipts,
    ) -> None:
        # The failure this is about is invisible from the result: `/bin/sh -c`
        # means the thing that matters is a *child* of the process the tool
        # holds, so killing only that leaves the child alive and reparented,
        # holding whatever port it had bound, with nothing left that knows it
        # exists. `start_new_session=True` plus `killpg` is what prevents it.
        result = await self._tool(entered, receipts, timeout_seconds=0.5).handle(
            _invocation(
                "project_run",
                command="sh -c 'echo $$ > child.pid; sleep 30' & sleep 30",
            )
        )
        assert result.error is not None
        recorded = (project / "child.pid").read_text().strip()
        for _ in range(50):
            try:
                os.kill(int(recorded), 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:  # pragma: no cover - only reached when the group survived
            pytest.fail(f"process {recorded} outlived the command that started it")

    async def test_too_much_output_kills_the_command_and_says_so(
        self,
        entered: ProjectFileScope,
        monkeypatch: pytest.MonkeyPatch,
        receipts: ReadReceipts,
    ) -> None:
        # Reading stops at the ceiling, which fills the pipe and blocks the
        # command; the kill is what turns "we stopped listening" into "it
        # stopped talking". Without it the `wait()` below would be waiting on
        # something that is waiting on us.
        monkeypatch.setattr(project_files, "MAX_CAPTURE_BYTES", 4096)
        result = await self._tool(entered, receipts, timeout_seconds=20.0).handle(
            _invocation("project_run", command="yes agent-workbench")
        )
        assert result.error is None
        assert result.content is not None
        assert "was killed after producing more than" in result.content

    def test_it_is_destructive_and_says_which_scope_it_needs(self) -> None:
        # `destructive` rather than `external`, and the gap is the whole point:
        # `code.external_requires_approval` defaults to False, so an `external`
        # tool would run ungated by default. `destructive` is armed in every
        # Code envelope regardless.
        spec = ProjectRunTool(ProjectFileScope(), ReadReceipts(), environment={}).spec()
        assert spec.risk == "destructive"
        assert spec.concurrency == "exclusive"
        assert spec.permission_scopes == ("project:run",)

    def test_its_binding_records_nothing_in_the_ledger(self) -> None:
        # A keyed binding would be refused by ADR-075's `advertise` guardrail
        # before a model ever saw it, and would stop the API process from
        # assembling at all -- the Code gateway is built with no ledger. What
        # keeps a command from running twice is that a Code turn is never
        # replayed, and that every call stops at a human.
        assert (
            ProjectRunTool(ProjectFileScope(), ReadReceipts(), environment={})
            .binding()
            .operation_key
            is None
        )


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
