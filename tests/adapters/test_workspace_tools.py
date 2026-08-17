"""The workspace tools (ADR-028 stage 1 PR-1.3; ADR-030 §2.3 for edit).

Every refusal is paired with the control that must still succeed, and the
schemas are checked against the gateway's own validator: a tool this repository
ships must pass the same gate a third-party one does.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.workspace import (
    _UNWRITABLE_MEDIA_TYPES,
    _UNWRITABLE_SUFFIXES,
    WorkspaceEditTool,
    WorkspaceGrepTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceUnavailableError,
    WorkspaceWriteTool,
)
from agent_workbench.application.workspace import Workspace, WorkspaceSession
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.runtime.schema_validation import assert_schema_supported

TENANT = "tenant_local"
OWNER = "user_local"


@contextmanager
def entered() -> Generator[WorkspaceScope]:
    """A scope with a session in it, the way a node invocation supplies one."""

    scope = WorkspaceScope()
    session = WorkspaceSession(
        workspace=Workspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        )
    )
    with scope.using(session):
        yield scope


def invoke(tool: object, **arguments: object) -> object:
    call = ToolCall(
        tool_call_id="toolu_" + "0" * 20,
        tool_name=tool.binding().spec.name,
        arguments=dict(arguments),
    )
    invocation = ToolInvocation(
        call=call,
        context=ExecutionContext(
            principal=PrincipalContext(tenant_id=TENANT, principal_id=OWNER),
            envelope=AuthorizationEnvelope(),
            agent_run_id="run_" + "0" * 28,
            policy_identity="test",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=30,
    )
    return asyncio.run(tool.handle(invocation))


def version_of(scope: WorkspaceScope) -> str | None:
    session = scope.current()
    assert session is not None
    return session.version


#: A zip header, which is what the first bytes of a .docx are. The NUL is the
#: byte that matters and it is where a real package puts it.
DOCX_HEADER = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00"


def put_bytes(scope: WorkspaceScope, name: str, content: bytes) -> None:
    """Bind raw bytes, the way an MCP result reaches the working set.

    Not through `workspace_write`, which stores text and now refuses these
    names outright. A rendered document arrives from a tool that stored it
    itself, so this is the only way a binary entry exists at all.
    """

    session = scope.current()
    assert session is not None
    session.version = asyncio.run(
        session.workspace.write(
            session.version,
            name,
            content,
            media_type=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
        )
    )


def test_every_workspace_schema_passes_the_gateway_validator() -> None:
    # The subset this repository enforces is deliberately small. A tool it ships
    # that could not pass its own gate would be found at gateway assembly, i.e.
    # at process start, which is a bad place to learn it.
    with entered() as scope:
        for tool in (
            WorkspaceListTool(scope),
            WorkspaceReadTool(scope),
            WorkspaceWriteTool(scope),
            WorkspaceEditTool(scope),
            WorkspaceGrepTool(scope),
        ):
            spec = tool.binding().spec
            assert_schema_supported(spec.input_schema, origin=f"tool {spec.name}")


def test_a_write_advances_the_session_version_so_the_node_can_commit_it() -> None:
    with entered() as scope:
        assert version_of(scope) is None

        result = invoke(WorkspaceWriteTool(scope), name="notes.md", content="hello")

        assert result.status == "ok"
        assert version_of(scope) is not None


def test_a_write_names_the_file_it_produced_as_a_field_not_a_sentence() -> None:
    """ADR-063: the fact travels structured, so no consumer has to parse prose.

    ``content`` says the same thing in English, and that sentence is what a
    console had to regex before this. It is not pinned by any test, so it was
    free to be reworded at any time -- which is the definition of not being a
    contract.
    """

    with entered() as scope:
        result = invoke(WorkspaceWriteTool(scope), name="report.md", content="hello")

        assert result.workspace_writes == ("report.md",)


def test_an_edit_names_the_file_it_rebound() -> None:
    """An edit replaces the bytes a name points at, so it produced that file
    just as much as a write did."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="report.md", content="alpha beta")

        result = invoke(
            WorkspaceEditTool(scope),
            name="report.md",
            old_text="alpha",
            new_text="gamma",
        )

        assert result.workspace_writes == ("report.md",)


def test_a_refused_write_names_nothing_because_nothing_was_written() -> None:
    """The failure direction, for both tools.

    A field that reported the *attempted* name would put a file in the console
    that does not exist in the workspace, and no later listing would remove it.
    Empty here is a consequence of returning before the version advances, not
    of remembering to clear anything.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="report.md", content="alpha")

        refused_write = invoke(WorkspaceWriteTool(scope), name="../escape", content="x")
        refused_edit = invoke(
            WorkspaceEditTool(scope),
            name="report.md",
            old_text="not in the file",
            new_text="x",
        )

        assert refused_write.status == "error"
        assert refused_write.workspace_writes == ()
        assert refused_edit.status == "error"
        assert refused_edit.workspace_writes == ()


def test_a_read_reports_no_writes_because_it_performed_none() -> None:
    """The control: the field means "this call wrote these", not "these exist"."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="report.md", content="hello")

        result = invoke(WorkspaceReadTool(scope), name="report.md")

        assert result.status == "ok"
        assert result.workspace_writes == ()


def test_read_returns_what_write_put_there() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="notes.md", content="hello")

        result = invoke(WorkspaceReadTool(scope), name="notes.md")

        assert result.status == "ok"
        assert "hello" in result.content


def test_reading_a_rendered_document_describes_it_instead_of_decoding_it() -> None:
    # The bug this closes killed a whole Task, and not at this tool. Decoding a
    # .docx with errors="replace" yields mojibake containing \u0000; that text
    # became the model's prompt, the prompt was recorded in a `ModelStarted`
    # event, and PostgreSQL refused the insert -- `\u0000 cannot be converted
    # to text`. `task_d66f8ec0...` died at `review`, whose only mistake was
    # opening the document the writer had just rendered.
    with entered() as scope:
        put_bytes(scope, "mcp-result.docx", DOCX_HEADER + b"\x9c\xed" * 400)

        result = invoke(WorkspaceReadTool(scope), name="mcp-result.docx")

        assert result.status == "ok"
        assert "\x00" not in result.content
        # It says the file exists and is not empty, which is the question a
        # reviewer is actually asking of it.
        assert "mcp-result.docx" in result.content
        assert "814" in result.content


def test_grep_skips_a_binary_file_and_says_which_one() -> None:
    # Silently skipping it would answer "no matches" about a file that was
    # never opened, and the model would conclude the content is not there.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="notes.md", content="quarterly revenue")
        put_bytes(scope, "mcp-result.docx", DOCX_HEADER + b"\xff\xfe" * 200)

        found = invoke(WorkspaceGrepTool(scope), pattern="revenue")
        missed = invoke(WorkspaceGrepTool(scope), pattern="nothing-matches-this")

        assert found.status == "ok"
        assert "\x00" not in found.content
        assert "notes.md" in found.content
        assert "mcp-result.docx" in found.content
        # And on the empty side too, where "No matches" alone would be a lie by
        # omission about the one file it could not read.
        assert "mcp-result.docx" in missed.content


def test_editing_a_binary_file_is_refused_rather_than_corrupting_it() -> None:
    # A decode-splice-encode round trip through errors="replace" does not edit a
    # package, it destroys it -- and would report success for doing so.
    with entered() as scope:
        put_bytes(scope, "mcp-result.docx", DOCX_HEADER + b"\x9c\xed" * 100)
        after_write = version_of(scope)

        refused = invoke(
            WorkspaceEditTool(scope),
            name="mcp-result.docx",
            old_text="Q3",
            new_text="Q4",
        )

        assert refused.status == "error"
        assert refused.error is not None
        assert "corrupt" in refused.error.message
        assert version_of(scope) == after_write


def test_a_text_file_holding_no_nul_is_still_read_edited_and_searched() -> None:
    # The control for all three refusals above. They share one predicate, so a
    # predicate that answered "binary" too eagerly would silence the whole
    # working set at once.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="report.md", content="Q3 revenue rose")

        read = invoke(WorkspaceReadTool(scope), name="report.md")
        found = invoke(WorkspaceGrepTool(scope), pattern="revenue")
        edited = invoke(
            WorkspaceEditTool(scope), name="report.md", old_text="Q3", new_text="Q4"
        )

        assert "Q3 revenue rose" in read.content
        assert "report.md" in found.content
        assert edited.status == "ok"
        assert (
            "Q4 revenue rose"
            in invoke(WorkspaceReadTool(scope), name="report.md").content
        )


def test_reading_a_missing_name_is_a_failed_result_not_an_empty_one() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="present.md", content="x")

        missing = invoke(WorkspaceReadTool(scope), name="absent.md")
        present = invoke(WorkspaceReadTool(scope), name="present.md")

        assert missing.status == "error"
        assert present.status == "ok"


def test_a_path_shaped_name_is_refused_and_the_version_does_not_move() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="ok.md", content="x")
        after_valid = version_of(scope)

        refused = invoke(WorkspaceWriteTool(scope), name="../escape", content="x")

        assert refused.status == "error"
        assert version_of(scope) == after_valid

        # Control: a legal name straight after the refusal still moves it.
        invoke(WorkspaceWriteTool(scope), name="escape.md", content="x")
        assert version_of(scope) != after_valid


def test_a_declared_word_type_is_refused_because_this_tool_writes_text() -> None:
    # The write went through and stored 600 bytes of Chinese prose under the
    # .docx media type, which the console reads as "this is a Word document":
    # it offered the layout preview, and the layout route answered 422. The
    # model was believed about a format its own content cannot be in.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="ok.md", content="x")
        after_valid = version_of(scope)

        refused = invoke(
            WorkspaceWriteTool(scope),
            name="summary.docx",
            content="2024 年第四季度总结\n\n收入同比增长。",
            media_type=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
        )

        assert refused.status == "error"
        assert refused.error is not None
        assert refused.error.code == "invalid_tool_input"
        # Actionable, not just "no": the model has text in hand and needs to be
        # told where it can go.
        assert "text" in refused.error.message
        # A refused write leaves the version where it was, so a node that then
        # succeeds commits only what actually landed.
        assert version_of(scope) == after_valid


def test_a_word_name_is_refused_even_with_no_media_type_declared() -> None:
    # What actually reached a user, 2026-08-12: asked for a Word report on a
    # deployment with no renderer, the model called this tool with
    # `name="DeepSeek-report.docx"` and no `media_type` at all. The guess fell
    # through to `text/plain`, the type check saw nothing wrong, and 3 154
    # bytes of Markdown were stored under a Word name. Everything downstream
    # reads the name: the listing, the attachment rail, the file it downloads
    # as. Refusing only the declared type left the whole lie intact.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="ok.md", content="x")
        after_valid = version_of(scope)

        refused = invoke(
            WorkspaceWriteTool(scope),
            name="DeepSeek-report.docx",
            content="DeepSeek 最新模型调研报告\n\n一、引言",
        )

        assert refused.status == "error"
        assert refused.error is not None
        assert refused.error.code == "invalid_tool_input"
        # It names the format it is refusing, not just "no".
        assert "wordprocessingml" in refused.error.message
        assert version_of(scope) == after_valid


def test_a_word_name_is_refused_even_when_the_declared_type_is_text() -> None:
    # The gap the two checks close between them. A model that has been told
    # "declaring that type is refused" can comply with the letter of it --
    # `text/plain` on a `.docx` name -- and produce the same broken file.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="ok.md", content="x")
        after_valid = version_of(scope)

        refused = invoke(
            WorkspaceWriteTool(scope),
            name="summary.DOCX",
            content="2024 年第四季度总结",
            media_type="text/plain",
        )

        assert refused.status == "error"
        assert version_of(scope) == after_valid


def test_every_refused_suffix_names_a_type_the_tool_also_refuses() -> None:
    # Two lists, one rule. A suffix mapped to a type outside the set would make
    # the refusal message describe a format this tool is willing to write.
    assert set(_UNWRITABLE_SUFFIXES.values()) <= _UNWRITABLE_MEDIA_TYPES


def test_a_text_name_the_word_check_must_not_catch_still_writes() -> None:
    # The control for both refusals above. `.docx` is a suffix, not a substring:
    # a name that merely contains one of these words is an ordinary text file.
    with entered() as scope:
        result = invoke(
            WorkspaceWriteTool(scope),
            name="docx-outline.md",
            content="what the document will contain",
        )

        assert result.status == "ok"


def test_a_declared_text_type_still_writes() -> None:
    # The control for the refusal above. It has to be the same argument in the
    # same position, or the refusal could be a write tool that stopped writing.
    with entered() as scope:
        result = invoke(
            WorkspaceWriteTool(scope),
            name="summary.md",
            content="2024 年第四季度总结",
            media_type="text/markdown",
        )

        assert result.status == "ok"
        assert "text/markdown" in invoke(WorkspaceListTool(scope)).content


def test_an_svg_name_is_typed_as_the_image_it_is() -> None:
    # Typed text/plain (the fallback) an .svg never reached the console's
    # `<img>` viewer, so a drawn diagram opened as its own markup. The guess
    # costs a label, and this is the one suffix where the wrong label hides
    # the picture.
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="chart.svg", content="<svg/>")

        assert "image/svg+xml" in invoke(WorkspaceListTool(scope)).content


def test_list_reports_names_sizes_and_types() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="b.md", content="two")
        invoke(WorkspaceWriteTool(scope), name="a.md", content="one")

        result = invoke(WorkspaceListTool(scope))

        assert result.status == "ok"
        # Sorted, so a listing shown to a model is the same for the same set.
        assert result.content.index("a.md") < result.content.index("b.md")
        assert "text/markdown" in result.content


def test_an_empty_workspace_lists_as_empty_rather_than_failing() -> None:
    with entered() as scope:
        assert invoke(WorkspaceListTool(scope)).status == "ok"


def test_the_write_tool_declares_the_risk_its_effects_have() -> None:
    with entered() as scope:
        binding = WorkspaceWriteTool(scope).binding()

        assert binding.spec.risk == "write"
        assert binding.spec.concurrency == "exclusive"
        assert binding.spec.permission_scopes == ("workspace:write",)
        # No operation key: the write lands in this project's own versioned
        # store, so a replay produces another version rather than a second
        # outside effect.
        assert binding.operation_key is None


def test_the_read_tools_are_safe_and_parallel() -> None:
    with entered() as scope:
        for tool in (WorkspaceListTool(scope), WorkspaceReadTool(scope)):
            spec = tool.binding().spec
            assert spec.risk == "read"
            assert spec.idempotency == "safe"
            assert spec.concurrency == "parallel"


def test_a_tool_outside_a_node_refuses_rather_than_inventing_a_workspace() -> None:
    # A workspace nothing committed is one no checkpoint names, so everything
    # written into it would be discarded at the end of the run in silence.
    with pytest.raises(WorkspaceUnavailableError):
        invoke(WorkspaceListTool(WorkspaceScope()))


def test_two_sessions_in_one_process_do_not_see_each_other() -> None:
    # Two Task lanes in one Worker process is the ordinary case since ADR-024.
    with entered() as first:
        invoke(WorkspaceWriteTool(first), name="first.md", content="1")
        first_version = version_of(first)

        with entered() as second:
            assert version_of(second) is None
            empty = invoke(WorkspaceListTool(second))
            assert empty.content == "The workspace is empty."

        assert version_of(first) == first_version


# --- workspace_edit: exactly one match, or nothing happens (ADR-030 2.3) -----


DOC = "alpha\nbeta\ngamma\n"


def read_back(scope: WorkspaceScope, name: str) -> str:
    result = invoke(WorkspaceReadTool(scope), name=name)
    assert result.status == "ok"
    return result.content


def test_an_edit_matching_once_replaces_only_that_passage() -> None:
    """The control the two refusals below are measured against.

    Without it, a tool that refused every edit would satisfy the whole rest of
    this section.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="doc.md", content=DOC)
        before = version_of(scope)

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="beta", new_text="BETA"
        )

        assert result.status == "ok"
        assert read_back(scope, "doc.md") == "alpha\nBETA\ngamma\n"
        assert version_of(scope) != before


def test_an_edit_matching_nothing_is_refused_and_changes_nothing() -> None:
    """Zero matches means the model believes the file says something it does not.

    The file assertion is the point. An error that still wrote would be worse
    than one that did not, because the model would be told it failed while the
    next reader saw a changed file.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="doc.md", content=DOC)
        before = version_of(scope)

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="delta", new_text="x"
        )

        assert result.status == "error"
        assert read_back(scope, "doc.md") == DOC
        assert version_of(scope) == before


def test_an_edit_matching_twice_is_refused_and_changes_nothing() -> None:
    """The most important one: editing "the first" would be silent corruption.

    Most editors replace the first occurrence. Here that is wrong -- the model
    did not know there were two, so "the first" is a position it never chose,
    and nothing in the result would tell it something else moved.
    """

    with entered() as scope:
        doubled = "repeat\nmiddle\nrepeat\n"
        invoke(WorkspaceWriteTool(scope), name="doc.md", content=doubled)
        before = version_of(scope)

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="repeat", new_text="once"
        )

        assert result.status == "error"
        assert read_back(scope, "doc.md") == doubled
        assert version_of(scope) == before


def test_the_refusal_says_how_many_times_it_matched() -> None:
    """A model that is told only "failed" retries the same edit.

    The count is what makes the next attempt different: two means disambiguate,
    zero means re-read.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="doc.md", content="repeat repeat repeat")

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="repeat", new_text="x"
        )

        assert result.error is not None
        assert "3 times" in result.error.message


def test_editing_a_file_that_is_not_there_is_not_found_rather_than_no_match() -> None:
    """Two different mistakes, and the model's next move differs by which.

    "No such file" sends it to workspace_list; "no match" sends it to
    workspace_read. Collapsing them into one error costs a step every time.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="other.md", content=DOC)

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="beta", new_text="x"
        )

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "not_found"


def test_an_edit_may_delete_a_passage() -> None:
    """Empty new_text is a replacement, not a missing argument."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="doc.md", content=DOC)

        result = invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="beta\n", new_text=""
        )

        assert result.status == "ok"
        assert read_back(scope, "doc.md") == "alpha\ngamma\n"


def test_a_later_read_of_an_edited_file_sees_the_edit() -> None:
    """The session advanced, so the next tool call in the same node sees it.

    Asserting through the read tool rather than the session version, because
    what a node actually depends on is the next tool seeing the new bytes.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="doc.md", content=DOC)
        invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="alpha", new_text="ALPHA"
        )
        invoke(
            WorkspaceEditTool(scope), name="doc.md", old_text="gamma", new_text="GAMMA"
        )

        assert read_back(scope, "doc.md") == "ALPHA\nbeta\nGAMMA\n"


def test_edit_outside_a_node_session_refuses_like_the_others() -> None:
    """Same rule as the other three: an unentered scope is not one to create."""

    tool = WorkspaceEditTool(WorkspaceScope())

    with pytest.raises(WorkspaceUnavailableError):
        invoke(tool, name="doc.md", old_text="a", new_text="b")


# --- workspace_grep: bounded, and interruptible (ADR-030 2.4) ---------------


def test_grep_reports_the_file_line_number_and_line() -> None:
    """A name alone would send the model back to read the whole file."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="a.md", content="alpha\nbeta\n")
        invoke(WorkspaceWriteTool(scope), name="b.md", content="gamma\n")

        result = invoke(WorkspaceGrepTool(scope), pattern="beta")

        assert result.status == "ok"
        assert "a.md:2: beta" in result.content


def test_grep_finding_nothing_says_so_rather_than_failing() -> None:
    """The control for the search above, and a distinct outcome from an error.

    "No matches" is an answer; an error is a tool the model should retry
    differently. Conflating them costs a step every time a search legitimately
    comes up empty.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="a.md", content="alpha\n")

        result = invoke(WorkspaceGrepTool(scope), pattern="nowhere")

        assert result.status == "ok"
        assert "No matches" in result.content


def test_a_name_glob_narrows_which_files_are_searched() -> None:
    """And the control: without the glob, the same pattern finds both."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="notes.md", content="target\n")
        invoke(WorkspaceWriteTool(scope), name="data.csv", content="target\n")

        narrowed = invoke(WorkspaceGrepTool(scope), pattern="target", name_glob="*.md")
        both = invoke(WorkspaceGrepTool(scope), pattern="target")

        assert "notes.md" in narrowed.content
        assert "data.csv" not in narrowed.content
        assert "notes.md" in both.content
        assert "data.csv" in both.content


def test_a_pattern_that_does_not_compile_is_refused_as_bad_input() -> None:
    """Named as the model's mistake, so the next attempt is a different pattern."""

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="a.md", content="alpha\n")

        result = invoke(WorkspaceGrepTool(scope), pattern="(unclosed")

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "invalid_tool_input"


def test_a_catastrophically_backtracking_pattern_is_interrupted() -> None:
    """The one that matters: the pattern is untrusted input.

    ``(a|a)*$`` against a long run of "a" is exponential, and stdlib ``re``
    would run it to completion however long that takes -- there is no way to
    interrupt it, so a Worker lane would be held indefinitely by one tool call.
    Measured on this engine at the shipped ceiling: it raises instead.

    The assertion is on wall clock as well as on the error, because an
    implementation that merely *reported* a timeout after finishing would
    satisfy an error-only assertion while still having hung.
    """

    with entered() as scope:
        # Inside MAX_GREP_LINE_CHARS on purpose. The line is truncated before
        # matching, and a subject cut short of its "b" would end in a run of
        # "a" that the pattern matches immediately -- the fixture has to blow
        # up on the text the engine is actually handed, not on the text on disk.
        invoke(WorkspaceWriteTool(scope), name="a.md", content="a" * 300 + "b\n")

        started = time.monotonic()
        result = invoke(WorkspaceGrepTool(scope), pattern="(a|a)*$")
        elapsed = time.monotonic() - started

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "tool_timeout"
        assert elapsed < 30, f"the scan was not interrupted: {elapsed:.1f}s"


def test_an_ordinary_pattern_is_not_slowed_by_the_timeout() -> None:
    """The control for the one above.

    Without it, a tool that timed out on everything -- or refused every
    pattern -- would satisfy the interruption test.
    """

    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="a.md", content="a" * 300 + "b\n")

        result = invoke(WorkspaceGrepTool(scope), pattern="a+b$")

        assert result.status == "ok"
        assert "a.md:1:" in result.content


def test_a_long_line_is_truncated_rather_than_returned_whole() -> None:
    """A single line must not be able to spend the context a listing saved."""

    with entered() as scope:
        invoke(
            WorkspaceWriteTool(scope),
            name="a.md",
            content="needle" + "x" * 5_000 + "\n",
        )

        result = invoke(WorkspaceGrepTool(scope), pattern="needle")

        assert result.status == "ok"
        assert len(result.content) < 1_000


def test_grep_outside_a_node_session_refuses_like_the_others() -> None:
    tool = WorkspaceGrepTool(WorkspaceScope())

    with pytest.raises(WorkspaceUnavailableError):
        invoke(tool, pattern="anything")
