"""The HTTP Chat mode contract, including legacy-client inference."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agent_workbench.apps.api.routes.chat import (
    AskRequest,
    _ensure_answer_mode_available,
)


def test_legacy_request_with_a_knowledge_base_infers_rag() -> None:
    request = AskRequest.model_validate(
        {"question": "What is in the handbook?", "knowledge_base_id": "kb_main"}
    )

    assert request.answer_mode == "rag"


def test_legacy_request_without_a_knowledge_base_infers_direct() -> None:
    request = AskRequest.model_validate({"question": "Say hello"})

    assert request.answer_mode == "direct"
    assert request.knowledge_base_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "question": "Say hello",
            "answer_mode": "direct",
            "knowledge_base_id": "kb_main",
        },
        {"question": "Use sources", "answer_mode": "rag"},
    ],
)
def test_explicit_mode_and_knowledge_scope_must_agree(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AskRequest.model_validate(payload)


def test_explicit_direct_and_rag_shapes_are_accepted() -> None:
    direct = AskRequest.model_validate(
        {"question": "Say hello", "answer_mode": "direct"}
    )
    rag = AskRequest.model_validate(
        {
            "question": "Use sources",
            "answer_mode": "rag",
            "knowledge_base_id": "kb_main",
        }
    )

    assert direct.answer_mode == "direct"
    assert rag.answer_mode == "rag"


def test_legacy_ungrounded_deployment_is_a_direct_only_ceiling() -> None:
    _ensure_answer_mode_available("ungrounded", "direct")

    with pytest.raises(HTTPException) as raised:
        _ensure_answer_mode_available("ungrounded", "rag")

    assert raised.value.status_code == 422
    assert raised.value.detail == "this deployment supports direct chat only"


@pytest.mark.parametrize("shape", ["fixed", "agentic", "routed"])
def test_retrieval_deployments_offer_both_turn_modes(shape: str) -> None:
    _ensure_answer_mode_available(shape, "direct")
    _ensure_answer_mode_available(shape, "rag")
