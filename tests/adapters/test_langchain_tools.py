"""A third-party tool, under this project's gateway rather than beside it.

``ports.tools`` and ``runtime.tool_gateway`` have both said since they were
written that "native handlers, MCP tools and LangChain tools all arrive as the
same binding, so there is exactly one place where a tool can be stopped". That
sentence was a claim with nothing behind it. These tests are the backing.

What is under test is not that LangChain works -- it is that a tool this project
did not write is subject to the same schema validation, the same policy, the
same risk ceiling and the same one-result-per-call-id rule as one it did. A tool
that brought its own escape from any of those would make the gateway a
convention rather than a boundary.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import tool

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.tools.langchain import binding_for
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.tools import ToolBinding
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway


@tool
def word_count(text: str) -> int:
    """Count the words in a passage."""

    return len(text.split())


@tool
def always_raises(text: str) -> str:
    """A tool that fails the way third-party tools actually fail."""

    raise RuntimeError(f"upstream rejected: {text} with key sk-secret")


def _binding(subject: object = word_count, **overrides: object) -> ToolBinding:
    fields: dict[str, object] = {
        "risk": "read",
        "concurrency": "parallel",
        "idempotency": "safe",
    }
    fields.update(overrides)
    return binding_for(subject, **fields)  # type: ignore[arg-type]


def _context(**overrides: object) -> ExecutionContext:
    base: dict[str, object] = {
        "principal": PrincipalContext(tenant_id="tenant_a", principal_id="user_1"),
        "envelope": AuthorizationEnvelope(
            allowed_tools=("word_count", "always_raises"), max_tool_risk="read"
        ),
        "agent_run_id": "run_1",
        "policy_identity": "policy-1:ffff",
    }
    base.update(overrides)
    return ExecutionContext.model_validate(base)


def _drive(binding: ToolBinding, arguments: dict[str, object], **kwargs: object):
    """Take one call through the whole gateway, as a real run would."""

    registry = StaticToolRegistry([binding])
    gateway = ToolGateway(
        registry=registry, policy=EnvelopePolicyEngine(registry=registry)
    )
    context = _context(**kwargs)
    sink = ScopedEventSink(
        InMemoryEventLog(), EventScope(stream_id="stream_1", run_id="run_1")
    )
    call = ToolCall(
        tool_call_id="toolu_01",
        tool_name=binding.spec.name,
        arguments=arguments,  # pyright: ignore[reportArgumentType]
    )

    async def run() -> ToolResult:
        await gateway.propose(call, sink=sink)
        prepared = await gateway.prepare(call, context=context, sink=sink)
        if isinstance(prepared, PreparedCall):
            prepared = await gateway.authorize(prepared, context=context, sink=sink)
        if not isinstance(prepared, PreparedCall):
            return prepared
        return await gateway.invoke(
            prepared,
            context=context,
            cancellation=NullCancellationToken(),
            sink=sink,
        )

    return asyncio.run(run())


# --------------------------------------------------------------------------
# It arrives as an ordinary binding
# --------------------------------------------------------------------------


def test_a_langchain_tool_runs_through_the_gateway() -> None:
    """The sentence in two module docstrings, as one assertion."""

    result = _drive(_binding(), {"text": "one two three"})

    assert result.status == "ok"
    assert result.content == "3"


def test_the_tools_own_schema_is_what_gets_enforced() -> None:
    """Not a schema this project wrote for it.

    A rewritten schema would be one the tool is not actually validating
    against, so the two would diverge the first time the tool changed.
    """

    schema = _binding().spec.input_schema

    assert schema["type"] == "object"
    assert "text" in schema["properties"]  # pyright: ignore[reportIndexIssue]
    assert schema["required"] == ["text"]


def test_arguments_the_schema_forbids_never_reach_the_tool() -> None:
    """Validation is this project's, whoever wrote the tool."""

    result = _drive(_binding(), {"text": "hello", "unexpected": 1})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_a_missing_required_argument_is_refused() -> None:
    result = _drive(_binding(), {})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


# --------------------------------------------------------------------------
# It is subject to the same rules
# --------------------------------------------------------------------------


def test_the_deployment_declares_the_risk_not_the_tool() -> None:
    """A tool describing its own risk decides how carefully it is treated.

    Declared here, the ceiling can refuse it -- which is the whole point of
    having one for a tool this project did not write.
    """

    binding = _binding(
        risk="destructive",
        concurrency="exclusive",
        idempotency="unsafe",
        # ToolSpec refuses a write, external or destructive tool with no scope,
        # which is the same rule applying to a tool nobody here wrote.
        permission_scopes=("danger:run",),
    )

    result = _drive(
        binding,
        {"text": "hello"},
        envelope=AuthorizationEnvelope(
            allowed_tools=("word_count",),
            max_tool_risk="read",
            approval_required_risks=(),
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "policy_denied"


def test_a_tool_outside_the_envelope_is_refused() -> None:
    result = _drive(
        _binding(),
        {"text": "hello"},
        envelope=AuthorizationEnvelope(allowed_tools=(), max_tool_risk="read"),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "policy_denied"


def test_a_raising_tool_becomes_a_result_not_an_exception() -> None:
    """The loop is owed exactly one result per call id.

    A third-party tool raising is the ordinary case, and a run that received an
    exception instead of a result would have a call id nothing ever answered.
    """

    result = _drive(_binding(always_raises), {"text": "hello"})

    assert result.status == "error"
    assert result.error is not None
    assert result.tool_call_id == "toolu_01"


def test_a_raising_tool_does_not_leak_its_message() -> None:
    """Only the exception's type crosses the boundary.

    A provider puts request bodies and keys in exception text, and that text
    would otherwise reach an event log and a model's context.
    """

    result = _drive(_binding(always_raises), {"text": "hello"})

    assert result.error is not None
    assert "RuntimeError" in result.error.message
    assert "sk-secret" not in result.error.message
    assert "upstream rejected" not in result.error.message


def test_a_tool_with_no_description_still_gets_one() -> None:
    """The specification travels into a model request.

    An empty description is a tool the model has no basis for choosing or
    avoiding, which is worse than a dull one.
    """

    class _Bare:
        name = "bare"
        description = ""
        args_schema = None

        async def ainvoke(self, arguments: dict[str, object]) -> str:
            del arguments
            return "ok"

    binding = _binding(_Bare())

    assert binding.spec.description
    # No declared arguments means none are accepted, rather than any.
    assert binding.spec.input_schema["additionalProperties"] is False


@pytest.mark.parametrize("arguments", [{"text": 1}, {"text": None}, {"text": []}])
def test_the_wrong_type_is_refused_before_the_tool_runs(
    arguments: dict[str, object],
) -> None:
    result = _drive(_binding(), arguments)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"
