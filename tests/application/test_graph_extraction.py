"""What an extractor may claim, and what happens when it cannot claim it.

The failure tests each pair with a success on the same service, so an
implementation that returned "nothing extracted" unconditionally could not
pass. The distinction the suite exists to protect is ``extracted``: an empty
chunk and a dead provider both store nothing, and only one of them is a fact
about the corpus.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.application.graph_extraction import (
    GraphExtractionService,
    graph_identity,
)
from agent_workbench.domain.knowledge_graph import (
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
    normalize_entity_name,
)
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime.fake_executor import FakeAgentExecutor

PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")
PASSAGE = "Team Marlin carries the Cinder rotation."


def _service(
    executor: object, *, timeout_seconds: float = 5.0
) -> GraphExtractionService:
    return GraphExtractionService(
        executor=executor,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        sink_for=lambda stream_id: ScopedEventSink(
            log=InMemoryEventLog(),
            scope=EventScope(stream_id=stream_id, run_id=stream_id),
        ),
    )


def _answer(**fields: object) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _run(service: GraphExtractionService, text: str = PASSAGE):
    return asyncio.run(service.extract(PRINCIPAL, text=text))


# --- normalisation and the merge key ----------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Team Marlin", "team marlin"),
        ("  team   marlin  ", "Team Marlin"),
        ('"Team Marlin"', "Team Marlin"),
        ("Team Marlin.", "team MARLIN"),
    ],
)
def test_names_that_differ_only_in_presentation_share_a_merge_key(
    left: str, right: str
) -> None:
    assert normalize_entity_name(left) == normalize_entity_name(right)


def test_two_different_names_do_not_share_a_merge_key() -> None:
    """The control: normalisation must not join what a corpus keeps apart."""

    assert normalize_entity_name("Team Marlin") != normalize_entity_name("Team Osprey")
    # Articles and plurals are deliberately *not* folded -- doing so is a
    # language decision this project has not made, and a wrong merge cannot be
    # noticed from outside.
    assert normalize_entity_name("the rotation") != normalize_entity_name("rotation")


# --- what survives extraction ------------------------------------------------


def test_a_clean_answer_becomes_entities_and_relations() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _answer(
            entities=[
                {"name": "Team Marlin", "entity_type": "team"},
                {"name": "Cinder rotation", "entity_type": "rotation"},
            ],
            relations=[
                {
                    "subject": "Team Marlin",
                    "object": "Cinder rotation",
                    "description": "Team Marlin carries the Cinder rotation.",
                }
            ],
        )
    )
    result = _run(_service(executor))

    assert result.extracted is True
    assert [e.name for e in result.extraction.entities] == [
        "Team Marlin",
        "Cinder rotation",
    ]
    assert len(result.extraction.relations) == 1
    assert len(executor.requests) == 1
    # Deny-shaped and toolless: an extractor reads a passage.
    assert executor.requests[0].tool_names == ()


def test_a_relation_reaching_outside_the_chunk_is_dropped() -> None:
    """An edge to something the chunk never named would create an entry point
    with no evidence behind it -- the merged-graph failure ADR-037 refuses.

    Dropped rather than repaired: inventing the missing entity would be this
    code making the claim.
    """

    extraction = ChunkExtraction(
        entities=(ExtractedEntity(name="Team Marlin", entity_type="team"),),
        relations=(
            ExtractedRelation(
                subject="Team Marlin", object="Cinder rotation", description="carries"
            ),
            ExtractedRelation(
                subject="Team Marlin", object="team marlin", description="is itself"
            ),
        ),
    )

    kept = extraction.relations_with_known_entities()

    # Only the edge whose *both* endpoints were listed survives, and matching
    # is by merge key rather than by literal text.
    assert [r.object for r in kept] == ["team marlin"]


def test_an_empty_answer_is_a_fact_about_the_chunk_not_a_failure() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _answer(entities=[], relations=[])
    )
    result = _run(_service(executor))

    assert result.extracted is True
    assert result.extraction.entities == ()


def test_an_empty_passage_is_not_sent_to_the_model_at_all() -> None:
    executor = FakeAgentExecutor(respond=lambda request: _answer(entities=[]))
    result = _run(_service(executor), text="   \n  ")

    assert result.extracted is True
    assert executor.requests == []


# --- the failure paths, each against a control -------------------------------


def test_a_framing_failure_earns_exactly_one_corrective_turn() -> None:
    answers = iter(
        [
            'here you go: {"entities": []}',
            _answer(entities=[{"name": "Team Marlin", "entity_type": "team"}]),
        ]
    )
    executor = FakeAgentExecutor(respond=lambda request: next(answers))
    result = _run(_service(executor))

    assert result.extracted is True
    assert [e.name for e in result.extraction.entities] == ["Team Marlin"]
    assert len(executor.requests) == 2


def test_two_framing_failures_report_that_nothing_was_read() -> None:
    executor = FakeAgentExecutor(respond=lambda request: "not json, ever")
    result = _run(_service(executor))

    assert result.extracted is False
    assert result.extraction.entities == ()
    assert len(executor.requests) == 2


def test_a_wrong_claim_is_not_re_asked() -> None:
    """ADR-034's boundary. The framing test above is the control: two runs
    there, one here."""

    executor = FakeAgentExecutor(
        respond=lambda request: _answer(entities=[{"name": "Team Marlin"}])
    )
    result = _run(_service(executor))

    assert result.extracted is False
    assert len(executor.requests) == 1


def test_a_timeout_does_not_take_the_document_down_with_it() -> None:
    class _Hanging:
        async def run(self, request, emit, cancellation):
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    result = _run(_service(_Hanging(), timeout_seconds=0.05))

    assert result.extracted is False


def test_a_raising_executor_reports_rather_than_propagates() -> None:
    class _Raising:
        async def run(self, request, emit, cancellation):
            raise RuntimeError("provider exploded")

    result = _run(_service(_Raising()))

    assert result.extracted is False


def test_more_entities_than_the_ceiling_are_refused_not_truncated() -> None:
    """A chunk that "names" fifty things produced a list of words, and every
    one of them would become a nomination. Refused as a wrong claim -- which
    means no corrective turn, per the test above."""

    executor = FakeAgentExecutor(
        respond=lambda request: _answer(
            entities=[{"name": f"thing {i}", "entity_type": "thing"} for i in range(40)]
        )
    )
    result = _run(_service(executor))

    assert result.extracted is False


# --- identity ----------------------------------------------------------------


def test_graph_identity_changes_with_every_part_that_changes_the_graph() -> None:
    base = graph_identity(
        extraction_model="deepseek-chat", prompt_version="v1", embedder_identity="bge@1"
    )

    assert base != graph_identity(
        extraction_model="other", prompt_version="v1", embedder_identity="bge@1"
    )
    assert base != graph_identity(
        extraction_model="deepseek-chat",
        prompt_version="v2",
        embedder_identity="bge@1",
    )
    assert base != graph_identity(
        extraction_model="deepseek-chat",
        prompt_version="v1",
        embedder_identity="bge@2",
    )
