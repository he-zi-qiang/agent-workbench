"""Which citations an answer earned, and which it merely claimed.

Two failure modes are worth separating. Over-claiming is what this replaces:
reporting the retrieval packet as the answer's sources, so an answer that used
one passage shipped with seven beneath it. Fabrication is the one with teeth --
a chunk id the model produced and was never shown is a string, and echoing it
back as a source lends it this system's authority.

The verifier is pure, so every case is a value.
"""

from __future__ import annotations

from agent_workbench.application.citations import cited_chunk_ids, verify_citations
from agent_workbench.domain.context import Citation, ContextChunk, ContextPacket

#: Real-shaped ids. The verifier reads the shape the ingestion service mints, so
#: a test using "chunk_a" would exercise a pattern nothing in this system
#: produces -- and would keep passing if the two drifted apart.
A = "chk_" + "a" * 32
B = "chk_" + "b" * 32
C = "chk_" + "c" * 32
ELSEWHERE = "chk_" + "e" * 32


def _packet(*chunk_ids: str, document: str = "doc_1") -> ContextPacket:
    return ContextPacket(
        chunks=tuple(
            ContextChunk(
                chunk_id=chunk_id,
                document_id=document,
                document_version="v1",
                tenant_id="tenant_a",
                text="Deletions are tombstoned.",
            )
            for chunk_id in chunk_ids
        ),
        citations=tuple(
            Citation(document_id=document, chunk_id=chunk_id, document_version="v1")
            for chunk_id in chunk_ids
        ),
    )


# --------------------------------------------------------------------------
# What an answer names
# --------------------------------------------------------------------------


def test_the_ids_an_answer_names_are_read_in_first_mention_order() -> None:
    assert cited_chunk_ids(f"See [{B}] and [{A}], and again [{B}].") == (B, A)


def test_bracketed_prose_is_not_a_citation() -> None:
    """The control group, and the reason the pattern knows the id's shape.

    Brackets appear in ordinary writing. A scan that accepted any bracketed word
    would report "[redacted]" as an invented source, and the fabrication signal
    would drown in punctuation.
    """

    assert cited_chunk_ids("The answer is [redacted] and [see below].") == ()
    assert cited_chunk_ids("No sources at all.") == ()


def test_a_citation_counts_in_whatever_the_model_wrapped_it_in() -> None:
    """Found by the first live run, not by reading the prompt.

    The prompt labels evidence as ``[chunk_id]``, so the scan required brackets.
    Asked to cite, DeepSeek used parentheses -- and an answer that plainly named
    its source came back with none. The delimiter was never the signal.
    """

    for phrasing in (
        f"see [{A}]",
        f"see ({A})",
        f"see `{A}`",
        f"see {A}.",
        f"Source: {A}",
    ):
        assert cited_chunk_ids(phrasing) == (A,), phrasing


# --------------------------------------------------------------------------
# What survives checking
# --------------------------------------------------------------------------


def test_a_citation_the_answer_was_shown_survives() -> None:
    verdict = verify_citations(f"Yes [{A}].", (_packet(A, B),))

    assert tuple(c.chunk_id for c in verdict.verified) == (A,)
    assert verdict.fabricated == ()
    assert verdict.trustworthy is True


def test_a_passage_the_answer_ignored_is_not_reported_as_a_source() -> None:
    """The over-claim this replaces.

    Both chunks were retrieved and both are fenced; only one was named, so only
    one is offered. Reporting the other would put a source under a claim it was
    never drawn from, and a reader checking it finds nothing.
    """

    verdict = verify_citations(f"Yes [{A}].", (_packet(A, B),))

    assert len(verdict.verified) == 1


def test_an_id_the_answer_was_never_shown_is_dropped_not_echoed() -> None:
    """The one with teeth.

    A guessed identifier may collide with a real chunk somewhere this asker
    cannot read. Returning it as a source would be this system asserting a
    passage it never retrieved.
    """

    verdict = verify_citations(f"Yes [{A}] and [{ELSEWHERE}].", (_packet(A),))

    assert tuple(c.chunk_id for c in verdict.verified) == (A,)
    assert verdict.fabricated == (ELSEWHERE,)
    assert verdict.trustworthy is False


def test_an_answer_that_cites_nothing_earns_nothing() -> None:
    verdict = verify_citations(
        "The acquisition closes on the fourteenth.", (_packet(A),)
    )

    assert verdict.verified == ()
    assert verdict.fabricated == ()
    # Not naming a source is not the same as inventing one.
    assert verdict.trustworthy is True


def test_evidence_from_every_search_counts_not_only_the_last() -> None:
    """The agentic shape hands over one packet per search."""

    verdict = verify_citations(
        f"Both [{A}] and [{C}].",
        (_packet(A), _packet(B), _packet(C, document="doc_2")),
    )

    assert {c.chunk_id for c in verdict.verified} == {A, C}
    assert verdict.fabricated == ()


def test_a_run_shown_nothing_can_verify_nothing() -> None:
    verdict = verify_citations(f"Yes [{A}].", ())

    assert verdict.verified == ()
    assert verdict.fabricated == (A,)
