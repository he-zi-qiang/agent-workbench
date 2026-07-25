"""Serialization contract shared by every domain aggregate.

The suite is introspective on purpose: it discovers aggregates instead of
listing them, so a new one cannot be added without also being serialized,
version-checked and pinned to the golden payload.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import agent_workbench.domain as domain_package
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.context import (
    Citation,
    ContextChunk,
    ContextPacket,
    SourceLocator,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import EventEnvelope, RunStarted
from agent_workbench.domain.messages import Message, assistant_message, user_message
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PolicyDecision,
    PrincipalContext,
)
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION, VersionedModel
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec

GOLDEN_FILE = Path(__file__).parent / "golden" / "domain_v1.json"

TIMESTAMP = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)

ARTIFACT = ArtifactRef(
    artifact_id="art_0000000000000000000000000000001",
    tenant_id="tenant_demo",
    kind="report",
    media_type="application/pdf",
    size_bytes=2048,
    sha256="a" * 64,
    filename="research-report.pdf",
)
TOOL_CALL = ToolCall(
    tool_call_id="toolu_01demo",
    tool_name="knowledge_search",
    arguments={"query": "vector index", "top_k": 8},
    model_call_id="mc_0000000000000000000000000000001",
)
CHUNK = ContextChunk(
    chunk_id="chunk_1",
    document_id="doc_1",
    document_version="v3",
    tenant_id="tenant_demo",
    text="Qdrant performs one dense and sparse fusion per query.",
    locator=SourceLocator(page=4, paragraph=2),
    score=0.83,
)
CITATION = Citation(
    chunk_id="chunk_1",
    document_id="doc_1",
    document_version="v3",
    locator=SourceLocator(page=4, paragraph=2),
    quote="one dense and sparse fusion per query",
)
PRINCIPAL = PrincipalContext(
    principal_id="user_demo",
    tenant_id="tenant_demo",
    scopes=("knowledge:read", "artifact:write"),
)
ENVELOPE = AuthorizationEnvelope(
    allowed_tools=("export_artifact", "knowledge_search"),
    denied_tools=("shell",),
    max_tool_risk="write",
)
BUDGET = RunBudget(
    max_steps=8,
    max_tool_calls=24,
    max_total_tokens=120_000,
    max_cost_micro_usd=500_000,
    deadline=TIMESTAMP,
)
USAGE = BudgetUsage(
    steps=2,
    tool_calls=3,
    tokens=TokenUsage(input_tokens=1200, output_tokens=340, cache_read_tokens=900),
    cost_micro_usd=4200,
)

SAMPLES: dict[str, VersionedModel] = {
    "ArtifactRef": ARTIFACT,
    "ToolSpec": ToolSpec(
        name="export_artifact",
        description="Write the approved report to the artifact store.",
        input_schema={"type": "object", "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object"},
        concurrency="exclusive",
        risk="write",
        idempotency="keyed",
        timeout_seconds=120,
        permission_scopes=("artifact:write",),
    ),
    "ToolCall": TOOL_CALL,
    "ToolResult": ToolResult.succeeded(
        TOOL_CALL,
        content="3 passages retrieved",
        artifact=ARTIFACT,
        duration_ms=412,
    ),
    "PrincipalContext": PRINCIPAL,
    "AuthorizationEnvelope": ENVELOPE,
    "ExecutionContext": ExecutionContext(
        principal=PRINCIPAL,
        envelope=ENVELOPE,
        agent_run_id="run_0000000000000000000000000000001",
        policy_identity="2026-07-a:9f2c1d4b5e6a7b8c",
        task_id="task_0000000000000000000000000000001",
        workflow_thread_id="thread_000000000000000000000000000001",
        graph_node_id="node_research_internal",
        lease_epoch=7,
    ),
    "PolicyDecision": PolicyDecision.allow_modified(
        "argument_clamped",
        {"query": "vector index", "top_k": 8},
    ),
    "ContextPacket": ContextPacket(
        chunks=(CHUNK,),
        citations=(CITATION,),
        retrieval_trace_id="trace_1",
        token_estimate=96,
    ),
    "Message": assistant_message(text="Looking that up.", tool_calls=(TOOL_CALL,)),
    "AgentRunRequest": AgentRunRequest(
        trace=TraceContext(agent_run_id="run_0000000000000000000000000000001"),
        run_kind="chat",
        stream_id="stream_0000000000000000000000000000001",
        principal=PRINCIPAL,
        envelope=ENVELOPE,
        budget=BUDGET,
        system_prompt="Answer only from the provided context.",
        messages=(user_message("Which component owns hybrid fusion?"),),
        tool_names=("knowledge_search",),
    ),
    "AgentOutcome": AgentOutcome(
        agent_run_id="run_0000000000000000000000000000001",
        status="completed",
        stop_reason="completed",
        output_text="Qdrant owns fusion.",
        citations=(CITATION,),
        usage=USAGE,
    ),
    "EventEnvelope": EventEnvelope.for_payload(
        RunStarted(
            run_kind="chat",
            model_profile="main",
            tool_names=("knowledge_search",),
            budget=BUDGET,
        ),
        stream_id="stream_0000000000000000000000000000001",
        run_id="run_0000000000000000000000000000001",
        timestamp=TIMESTAMP,
        sequence=1,
        event_id="evt_0000000000000000000000000000001",
    ),
}


def _discover_aggregates() -> set[type[VersionedModel]]:
    """Import every domain module, then collect its VersionedModel subclasses.

    The result is filtered by module rather than taken from
    ``__subclasses__()`` wholesale: ports and adapters define aggregates too,
    and picking them up here would make this test depend on which other test
    module happened to be imported first.
    """

    for module in pkgutil.iter_modules(domain_package.__path__):
        importlib.import_module(f"{domain_package.__name__}.{module.name}")

    found: set[type[VersionedModel]] = set()

    def visit(model: type[VersionedModel]) -> None:
        for subclass in model.__subclasses__():
            if subclass.__module__.startswith(f"{domain_package.__name__}."):
                found.add(subclass)
            visit(subclass)

    visit(VersionedModel)
    return found


def test_samples_cover_every_domain_aggregate() -> None:
    discovered = {model.__name__ for model in _discover_aggregates()}
    assert discovered == set(SAMPLES), (
        "every serialized aggregate needs a sample so the round-trip, version "
        "and golden checks below actually cover it"
    )


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_aggregate_declares_the_current_schema_version(name: str) -> None:
    payload = SAMPLES[name].model_dump(mode="json")
    assert payload["schema_version"] == DOMAIN_SCHEMA_VERSION


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_json_round_trip_preserves_equality(name: str) -> None:
    sample = SAMPLES[name]
    restored = type(sample).model_validate(json.loads(sample.model_dump_json()))
    assert restored == sample


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_aggregate_rejects_a_foreign_schema_version(name: str) -> None:
    sample = SAMPLES[name]
    payload: dict[str, Any] = sample.model_dump(mode="json")
    payload["schema_version"] = DOMAIN_SCHEMA_VERSION + 1

    with pytest.raises(ValidationError):
        type(sample).model_validate(payload)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_aggregate_rejects_unknown_fields(name: str) -> None:
    sample = SAMPLES[name]
    payload: dict[str, Any] = sample.model_dump(mode="json")
    payload["unexpected_field"] = "drifted"

    with pytest.raises(ValidationError):
        type(sample).model_validate(payload)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_aggregate_is_immutable(name: str) -> None:
    sample = SAMPLES[name]
    field = next(iter(type(sample).model_fields))

    with pytest.raises(ValidationError):
        setattr(sample, field, None)


def test_serialization_matches_the_committed_golden_payloads() -> None:
    """Field renames and shape changes must be deliberate, not incidental."""

    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    current = {
        name: json.loads(sample.model_dump_json()) for name, sample in SAMPLES.items()
    }
    assert current == golden


def test_golden_payloads_load_back_into_equal_objects() -> None:
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    for name, sample in SAMPLES.items():
        assert type(sample).model_validate(golden[name]) == sample


SECRET_KEY_MARKERS = (
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "dsn",
    "password",
    "secret",
)


def _field_names(payload: object) -> list[str]:
    if isinstance(payload, dict):
        mapping: dict[str, Any] = payload  # pyright: ignore[reportAssignmentType]
        names: list[str] = []
        for key, value in mapping.items():
            names.append(key)
            names.extend(_field_names(value))
        return names
    if isinstance(payload, list):
        items: list[Any] = payload  # pyright: ignore[reportAssignmentType]
        return [name for item in items for name in _field_names(item)]
    return []


def test_no_aggregate_serializes_a_secret_like_field() -> None:
    """Domain payloads carry references and digests, never credentials.

    Token *counts* are legitimate, so this checks field names against the same
    markers the configuration redactor uses rather than scanning raw text.
    """

    for name, sample in SAMPLES.items():
        for field in _field_names(json.loads(sample.model_dump_json())):
            normalized = field.lower()
            offending = [
                marker for marker in SECRET_KEY_MARKERS if marker in normalized
            ]
            assert not offending, f"{name}.{field} looks like a secret field"


def test_message_sample_keeps_provider_tool_call_ids() -> None:
    message = SAMPLES["Message"]
    assert isinstance(message, Message)
    assert message.tool_calls()[0].tool_call_id == TOOL_CALL.tool_call_id


def test_error_info_is_not_an_aggregate() -> None:
    """ErrorInfo travels inside results and events, so it stays unversioned."""

    assert not issubclass(ErrorInfo, VersionedModel)
