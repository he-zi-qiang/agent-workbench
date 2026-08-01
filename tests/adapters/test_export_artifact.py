"""The one tool in v1 that writes.

Two properties carry the weight here. The bytes are a pure function of the
inputs, so two attempts at one export cannot disagree about what the report
is -- which is what makes "only one report exists" checkable rather than
asserted. And the operation key names the Task rather than the call, so a
resumed graph asks the ledger about the export it already tried instead of
minting a fresh one.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.memory.artifact_store import InMemoryArtifactStore
from agent_workbench.adapters.tools.export_artifact import (
    MAX_DRAFT_BYTES,
    SPEC,
    TOOL_NAME,
    ExportArtifactTool,
    operation_key_for,
    render_report,
)
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation

TENANT = "tenant_a"
OWNER = "user_1"


def _context(**overrides: object) -> ExecutionContext:
    base: dict[str, object] = {
        "principal": PrincipalContext(tenant_id=TENANT, principal_id=OWNER),
        "envelope": AuthorizationEnvelope(),
        "agent_run_id": "run_1",
        "policy_identity": "rev_1:ffff",
        "task_id": "task_1",
        "lease_epoch": 3,
    }
    base.update(overrides)
    return ExecutionContext.model_validate(base)


def _call(**arguments: str) -> ToolCall:
    return ToolCall(
        tool_call_id="toolu_01",
        tool_name=TOOL_NAME,
        arguments={"draft_ref": "art_draft_1", "approval_id": "apr_1", **arguments},
    )


def _invocation(call: ToolCall, context: ExecutionContext) -> ToolInvocation:
    return ToolInvocation(
        call=call,
        context=context,
        cancellation=NullCancellationToken(),
        timeout_seconds=SPEC.timeout_seconds,
    )


async def _store_draft(store: InMemoryArtifactStore, body: bytes) -> str:
    reference = await store.put(
        tenant_id=TENANT,
        owner_id=OWNER,
        kind="report",
        media_type="text/markdown",
        content=body,
    )
    return reference.artifact_id


# --------------------------------------------------------------------------
# What the specification commits to
# --------------------------------------------------------------------------


def test_the_specification_declares_a_ledgered_write() -> None:
    """Each of these is load-bearing somewhere else.

    ``write`` is what the submitted envelope's ceiling is measured against,
    ``exclusive`` is what keeps it from running beside anything, and anything
    other than ``keyed`` either forbids the operation key (``safe``) or claims
    a repeat is unsafe even when the ledger answered it (``unsafe``).
    """

    assert SPEC.risk == "write"
    assert SPEC.concurrency == "exclusive"
    assert SPEC.idempotency == "keyed"
    assert SPEC.permission_scopes == ("artifact:export",)


def test_the_binding_carries_an_operation_key() -> None:
    """Without this the gateway dispatches it unledgered, and never says so."""

    binding = ExportArtifactTool(artifacts=InMemoryArtifactStore()).binding()

    assert binding.operation_key is not None


def test_the_operation_key_names_the_task_not_the_call() -> None:
    """A resumed graph mints a new tool_call_id for the same intent.

    Keying on the call would make every resume a fresh export, which is exactly
    the duplicate the ledger exists to prevent.
    """

    context = _context()
    first = operation_key_for(_call(), context)
    second = operation_key_for(
        _call().model_copy(update={"tool_call_id": "toolu_99"}), context
    )

    assert first == second == "export:task_1"


def test_the_operation_key_separates_tasks() -> None:
    assert operation_key_for(_call(), _context(task_id="task_2")) == "export:task_2"


# --------------------------------------------------------------------------
# What it writes
# --------------------------------------------------------------------------


def test_the_export_stores_a_report_containing_the_draft() -> None:
    async def scenario() -> None:
        store = InMemoryArtifactStore()
        draft_ref = await _store_draft(store, b"the body of the report")
        tool = ExportArtifactTool(artifacts=store)

        result = await tool.handle(_invocation(_call(draft_ref=draft_ref), _context()))

        assert result.status == "ok"
        assert result.artifact is not None
        assert result.artifact.kind == "report"
        stored = await store.get(
            tenant_id=TENANT,
            artifact_id=result.artifact.artifact_id,
            principal_id=OWNER,
        )
        assert b"the body of the report" in stored
        # The header names what authorised this and what it came from, so a
        # report found on disk is traceable without the database.
        assert b"task_1" in stored
        assert b"apr_1" in stored
        assert draft_ref.encode() in stored

    asyncio.run(scenario())


def test_the_rendered_bytes_are_a_function_of_the_inputs_alone() -> None:
    """No timestamp, no run id, no worker name.

    This is what makes a duplicate detectable. Two exports that disagreed on
    their bytes would leave two reports that are both plausibly "the" report,
    and no test could tell a retry from a second export.
    """

    first = render_report(
        task_id="task_1", approval_id="apr_1", draft_ref="art_1", draft=b"body"
    )
    second = render_report(
        task_id="task_1", approval_id="apr_1", draft_ref="art_1", draft=b"body"
    )

    assert first == second


def test_a_different_approval_renders_a_different_report() -> None:
    """The header is evidence, so it has to move when the facts move."""

    approved = render_report(
        task_id="task_1", approval_id="apr_1", draft_ref="art_1", draft=b"body"
    )
    other = render_report(
        task_id="task_1", approval_id="apr_2", draft_ref="art_1", draft=b"body"
    )

    assert approved != other


def test_a_corrupt_draft_is_exported_rather_than_refused() -> None:
    """A person already approved this. Undecodable bytes are a broken draft.

    Refusing here would turn a synthesis bug into an approval a human has to
    give again, and the replacement characters make the damage visible.
    """

    rendered = render_report(
        task_id="task_1", approval_id="apr_1", draft_ref="art_1", draft=b"\xff\xfe"
    )

    assert rendered.endswith("��".encode())


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_a_draft_this_principal_cannot_read_is_not_found() -> None:
    """The store refuses to distinguish absent from another owner's.

    Reporting the difference here would answer the question the store declined,
    so both arrive as one code.
    """

    async def scenario() -> None:
        store = InMemoryArtifactStore()
        draft_ref = await _store_draft(store, b"someone else's draft")
        tool = ExportArtifactTool(artifacts=store)

        result = await tool.handle(
            _invocation(
                _call(draft_ref=draft_ref),
                _context(
                    principal=PrincipalContext(tenant_id=TENANT, principal_id="user_2")
                ),
            )
        )

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "not_found"

    asyncio.run(scenario())


def test_an_absent_draft_is_the_same_answer() -> None:
    async def scenario() -> None:
        tool = ExportArtifactTool(artifacts=InMemoryArtifactStore())

        result = await tool.handle(
            _invocation(_call(draft_ref="art_nothing"), _context())
        )

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "not_found"

    asyncio.run(scenario())


def test_an_oversized_draft_is_refused_rather_than_exported() -> None:
    async def scenario() -> None:
        store = InMemoryArtifactStore()
        draft_ref = await _store_draft(store, b"x" * (MAX_DRAFT_BYTES + 1))
        tool = ExportArtifactTool(artifacts=store)

        result = await tool.handle(_invocation(_call(draft_ref=draft_ref), _context()))

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "output_too_large"

    asyncio.run(scenario())


def test_an_export_outside_a_task_is_invalid_input() -> None:
    """There would be no operation key, so there would be no ledger row."""

    async def scenario() -> None:
        tool = ExportArtifactTool(artifacts=InMemoryArtifactStore())

        result = await tool.handle(_invocation(_call(), _context(task_id=None)))

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "invalid_tool_input"

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["draft_ref", "approval_id"])
def test_a_missing_argument_is_refused_before_anything_is_written(
    missing: str,
) -> None:
    async def scenario() -> None:
        store = InMemoryArtifactStore()
        tool = ExportArtifactTool(artifacts=store)
        arguments = {"draft_ref": "art_1", "approval_id": "apr_1"}
        del arguments[missing]
        call = ToolCall(
            tool_call_id="toolu_01", tool_name=TOOL_NAME, arguments=arguments
        )

        result = await tool.handle(_invocation(call, _context()))

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "invalid_tool_input"

    asyncio.run(scenario())


def test_the_export_writes_under_the_context_identity_not_the_arguments() -> None:
    """The schema carries no identity, and this is why that matters.

    A tool that took an owner from its arguments would let model-produced text
    choose whose namespace the report lands in.
    """

    assert set(SPEC.input_schema["properties"]) == {"draft_ref", "approval_id"}
    assert SPEC.input_schema["additionalProperties"] is False

    async def scenario() -> None:
        store = InMemoryArtifactStore()
        draft_ref = await _store_draft(store, b"body")
        tool = ExportArtifactTool(artifacts=store)

        result = await tool.handle(_invocation(_call(draft_ref=draft_ref), _context()))

        assert result.artifact is not None
        assert result.artifact.tenant_id == TENANT
        # Readable by the context's principal, which is the only identity that
        # reached the store.
        await store.get(
            tenant_id=TENANT,
            artifact_id=result.artifact.artifact_id,
            principal_id=OWNER,
        )

    asyncio.run(scenario())
