"""Contract for the tool registry and the two side-effect-free tools."""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.tools import (
    StaticToolRegistry,
    read_document_tool,
    text_statistics_tool,
)
from agent_workbench.domain.errors import OperationCancelledError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import CancellationSource, NullCancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

CORPUS = {"doc_1": "Qdrant owns hybrid fusion.\nOne fusion per query."}
CONTEXT = ExecutionContext(
    principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
    envelope=AuthorizationEnvelope(allowed_tools=("read_document", "text_statistics")),
    agent_run_id="run_1",
    policy_identity="policy-v1:0e67f8dd84919551",
)


def _registry() -> StaticToolRegistry:
    return StaticToolRegistry([read_document_tool(CORPUS), text_statistics_tool()])


def _invoke(binding: ToolBinding, call: ToolCall) -> ToolResult:
    invocation = ToolInvocation(
        call=call,
        context=CONTEXT,
        cancellation=NullCancellationToken(),
        timeout_seconds=binding.spec.timeout_seconds,
    )
    return asyncio.run(binding.handler(invocation))


def test_an_unknown_tool_is_a_lookup_miss_not_an_exception() -> None:
    """The model proposed it, so it still has to receive one ToolResult."""

    assert _registry().get("definitely_not_registered") is None


def test_specifications_are_listed_in_a_stable_order() -> None:
    names = [spec.name for spec in _registry().specs()]

    assert names == ["read_document", "text_statistics"]


def test_a_duplicate_registration_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate tool registration"):
        StaticToolRegistry([read_document_tool(CORPUS), read_document_tool(CORPUS)])


def test_the_read_tool_returns_the_document() -> None:
    binding = _registry().get("read_document")
    assert binding is not None

    result = _invoke(
        binding,
        ToolCall(
            tool_call_id="toolu_1",
            tool_name="read_document",
            arguments={"document_id": "doc_1"},
        ),
    )

    assert result.status == "ok"
    assert result.content == CORPUS["doc_1"]


def test_a_missing_document_becomes_a_failed_result() -> None:
    binding = _registry().get("read_document")
    assert binding is not None

    result = _invoke(
        binding,
        ToolCall(
            tool_call_id="toolu_1",
            tool_name="read_document",
            arguments={"document_id": "doc_absent"},
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "not_found"


def test_a_wrongly_typed_argument_becomes_a_failed_result() -> None:
    binding = _registry().get("read_document")
    assert binding is not None

    result = _invoke(
        binding,
        ToolCall(
            tool_call_id="toolu_1",
            tool_name="read_document",
            arguments={"document_id": 7},
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_the_transform_tool_is_deterministic() -> None:
    binding = _registry().get("text_statistics")
    assert binding is not None

    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="text_statistics",
        arguments={"text": "one two\nthree"},
    )

    first = _invoke(binding, call)
    second = _invoke(binding, call)

    assert first.content == second.content
    assert first.content == "characters=13 words=3 lines=2"


def test_a_cancelled_run_stops_the_handler_before_it_works() -> None:
    binding = _registry().get("read_document")
    assert binding is not None
    cancellation = CancellationSource()
    cancellation.cancel("task cancelled")

    invocation = ToolInvocation(
        call=ToolCall(
            tool_call_id="toolu_1",
            tool_name="read_document",
            arguments={"document_id": "doc_1"},
        ),
        context=CONTEXT,
        cancellation=cancellation,
        timeout_seconds=5,
    )

    with pytest.raises(OperationCancelledError, match="task cancelled"):
        asyncio.run(binding.handler(invocation))


def test_read_tools_declare_themselves_parallel_and_safe() -> None:
    for spec in _registry().specs():
        assert spec.risk == "read"
        assert spec.concurrency == "parallel"
        assert spec.idempotency == "safe"
        assert spec.permission_scopes == ()
