"""Serialization contract for aggregates the ports introduce.

The domain suite guards the domain package by reflection; this one does the
same for ``agent_workbench.ports``, so a request or a stored row added to a
protocol cannot skip round-trip and version checks either.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from typing import Any

import pytest
from pydantic import ValidationError

import agent_workbench.ports as ports_package
from agent_workbench.domain.context import Citation
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION, VersionedModel
from agent_workbench.domain.tools import ToolSpec
from agent_workbench.ports.conversation_store import (
    ChatTurnClaim,
    ChatTurnResult,
    ConversationSession,
    StoredChatTurn,
    StoredMessage,
)
from agent_workbench.ports.documents import Document, DocumentVersion, UploadIntent
from agent_workbench.ports.model import ModelRequest
from agent_workbench.ports.outbox import OutboxEvent

COMPLETED_OUTCOME = AgentOutcome(
    agent_run_id="run_0000000000000000000000000000001",
    status="completed",
    stop_reason="completed",
    output_text="Qdrant owns fusion.",
)
TURN_RESULT = ChatTurnResult(
    outcome=COMPLETED_OUTCOME,
    answer="Qdrant owns fusion.",
    citations=(
        Citation(
            chunk_id="chunk_00000000000000000000000000001",
            document_id="doc_0000000000000000000000000000001",
            document_version="ver_0000000000000000000000000000001",
        ),
    ),
)
STORED_TURN = StoredChatTurn(
    turn_id="turn_000000000000000000000000000001",
    session_id="session_0000000000000000000000000000001",
    idempotency_key="request-1",
    request_hash="a" * 64,
    run_id=COMPLETED_OUTCOME.agent_run_id,
    status="release_pending",
    user_message_id="msg_0000000000000000000000000000001",
    result=TURN_RESULT,
)

SAMPLES: dict[str, VersionedModel] = {
    "ChatTurnClaim": ChatTurnClaim(
        turn=STORED_TURN,
        newly_claimed=False,
    ),
    "ChatTurnResult": TURN_RESULT,
    "UploadIntent": UploadIntent(
        upload_id="upl_0000000000000000000000000000001",
        tenant_id="tenant_demo",
        owner_id="user_demo",
        declared_size_bytes=2048,
        declared_sha256="a" * 64,
        media_type="application/pdf",
        filename="research-report.pdf",
    ),
    "DocumentVersion": DocumentVersion(
        version_id="ver_0000000000000000000000000000001",
        document_id="doc_0000000000000000000000000000001",
        source_revision=3,
        artifact_id="art_0000000000000000000000000000001",
        content_sha256="b" * 64,
    ),
    "Document": Document(
        document_id="doc_0000000000000000000000000000001",
        tenant_id="tenant_demo",
        owner_id="user_demo",
        knowledge_base_id="kb_main",
        source_revision=3,
    ),
    "OutboxEvent": OutboxEvent(
        sequence=7,
        event_id="obx_0000000000000000000000000000001",
        document_id="doc_0000000000000000000000000000001",
        source_revision=3,
        kind="document_upserted",
        payload={
            "tenant_id": "tenant_demo",
            "knowledge_base_id": "kb_main",
            "authorized_principals": ["user_demo"],
        },
        claim_token="clm_0000000000000000000000000000001",
    ),
    "ModelRequest": ModelRequest(
        model_profile="main",
        system_prompt="Answer only from the provided context.",
        messages=(user_message("Who owns hybrid fusion?"),),
        tools=(
            ToolSpec(
                name="read_document",
                description="Return the full text of one document.",
                input_schema={"type": "object"},
                concurrency="parallel",
                risk="read",
                idempotency="safe",
                timeout_seconds=5,
            ),
        ),
        max_output_tokens=4096,
    ),
    "ConversationSession": ConversationSession(
        session_id="session_0000000000000000000000000000001",
        tenant_id="tenant_demo",
        owner_id="user_demo",
        title="Hybrid retrieval",
    ),
    "StoredMessage": StoredMessage(
        message_id="msg_0000000000000000000000000000001",
        session_id="session_0000000000000000000000000000001",
        sequence=1,
        message=user_message("Who owns hybrid fusion?"),
    ),
    "StoredChatTurn": STORED_TURN,
}


def _discover_aggregates() -> set[type[VersionedModel]]:
    for module in pkgutil.iter_modules(ports_package.__path__):
        importlib.import_module(f"{ports_package.__name__}.{module.name}")

    found: set[type[VersionedModel]] = set()

    def visit(model: type[VersionedModel]) -> None:
        for subclass in model.__subclasses__():
            if subclass.__module__.startswith(f"{ports_package.__name__}."):
                found.add(subclass)
            visit(subclass)

    visit(VersionedModel)
    return found


def test_samples_cover_every_port_aggregate() -> None:
    discovered = {model.__name__ for model in _discover_aggregates()}

    assert discovered == set(SAMPLES)


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


def test_a_model_request_never_names_a_concrete_model() -> None:
    """Profile-to-model mapping is settings' decision, not a caller's."""

    payload = json.loads(SAMPLES["ModelRequest"].model_dump_json())

    assert payload["model_profile"] == "main"
    assert "model_id" not in payload
    assert "temperature" not in payload
    assert "api_key" not in payload
