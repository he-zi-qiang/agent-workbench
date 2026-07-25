"""Retrieved context and the rule that citations stay grounded in it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.domain.context import (
    Citation,
    ContextChunk,
    ContextPacket,
    SourceLocator,
)


def _chunk(chunk_id: str = "chunk_1", version: str = "v3") -> ContextChunk:
    return ContextChunk(
        chunk_id=chunk_id,
        document_id="doc_1",
        document_version=version,
        tenant_id="tenant_a",
        text="Qdrant owns hybrid fusion.",
        locator=SourceLocator(page=2, paragraph=1),
    )


def test_a_citation_must_point_at_a_chunk_in_the_packet() -> None:
    """An ungrounded citation cannot be re-verified against current ACLs."""

    with pytest.raises(ValidationError, match="not part of this packet"):
        ContextPacket(
            chunks=(_chunk(),),
            citations=(
                Citation(
                    chunk_id="chunk_missing",
                    document_id="doc_1",
                    document_version="v3",
                ),
            ),
        )


def test_a_citation_must_agree_with_the_chunk_document_version() -> None:
    with pytest.raises(ValidationError, match="disagrees with"):
        ContextPacket(
            chunks=(_chunk(version="v3"),),
            citations=(
                Citation(
                    chunk_id="chunk_1",
                    document_id="doc_1",
                    document_version="v4",
                ),
            ),
        )


def test_chunk_ids_are_unique_inside_a_packet() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ContextPacket(chunks=(_chunk(), _chunk()))


def test_a_grounded_packet_is_accepted() -> None:
    packet = ContextPacket(
        chunks=(_chunk(),),
        citations=(
            Citation(chunk_id="chunk_1", document_id="doc_1", document_version="v3"),
        ),
        retrieval_trace_id="trace_1",
        token_estimate=64,
    )

    assert packet.tenant_ids() == frozenset({"tenant_a"})


def test_a_mixed_tenant_packet_is_visible_to_the_caller() -> None:
    """Retrieval must never build one, so the check has to be cheap."""

    packet = ContextPacket(chunks=(_chunk("chunk_1"), _chunk("chunk_2")))
    other = ContextPacket(
        chunks=(
            _chunk("chunk_1"),
            ContextChunk(
                chunk_id="chunk_2",
                document_id="doc_2",
                document_version="v1",
                tenant_id="tenant_b",
                text="other tenant",
            ),
        )
    )

    assert len(packet.tenant_ids()) == 1
    assert len(other.tenant_ids()) == 2


def test_character_offsets_are_set_together_and_ordered() -> None:
    with pytest.raises(ValidationError, match="set together"):
        SourceLocator(char_start=10)
    with pytest.raises(ValidationError, match="must not precede"):
        SourceLocator(char_start=10, char_end=4)

    assert SourceLocator(char_start=4, char_end=10).char_end == 10


def test_pages_are_one_based() -> None:
    with pytest.raises(ValidationError):
        SourceLocator(page=0)
