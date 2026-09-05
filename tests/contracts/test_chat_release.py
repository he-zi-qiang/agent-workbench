"""Contract shared by the memory and PostgreSQL Chat release coordinators.

ADR-018 gives an unverified answer its own terminal event. That choice is made
once per turn, inside a release coordinator, from ``ChatTurnResult.grounded``
-- and it is the whole reason the second event type exists: the CLI and the web
console both branch on it, and an auditor reading the log has nothing else to
tell a checked answer from an unchecked one.

The two coordinators are therefore parameterized through the same scenarios.
The in-memory one is not a production component, but it is what almost every
Chat suite releases through, so a coordinator that answered ``AnswerCommitted``
for every publishable turn would leave those suites unable to fail on exactly
the regression this distinction exists to catch.
"""

from __future__ import annotations

from typing import Any

from harness import ChatReleaseHarness, StoreHarness

from agent_workbench.adapters.memory.chat_release import _withheld
from agent_workbench.adapters.persistence.chat_release import (
    PostgresChatReleaseCoordinator,
)
from agent_workbench.domain.context import Citation
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    ChatTurnResult,
    StoredChatTurn,
)
from agent_workbench.ports.event_log import EventScope

SESSION = "session_release_1"
TENANT = "tenant_a"
OWNER = "user_1"
RUN = "run_0000000000000000000000000000002"
KEY = "release-request-1"
REQUEST_HASH = "c" * 64
LEASE_SECONDS = 300
ANSWER = "the answer this turn produced"
REFUSAL = "The answer was withheld."
SCOPE = EventScope(stream_id=SESSION, run_id=RUN)
ANSWER_KINDS = frozenset(
    {"AnswerCommitted", "UngroundedAnswerCommitted", "AnswerWithheld"}
)


def _outcome() -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=RUN,
        status="completed",
        stop_reason="completed",
        output_text=ANSWER,
    )


def _candidate(*, grounded: bool) -> ChatTurnResult:
    """A publishable result that differs from its pair only in the flag.

    Deliberately: an ungrounded result may carry neither citations nor
    authorized revisions, so the only honest way to hold everything else equal
    is to give the grounded one none either -- a retrieval that returned
    nothing the answer cited. That is what makes the pair a real test of the
    branch: a coordinator that inferred groundedness from an empty citation
    list rather than reading the flag would answer ``UngroundedAnswerCommitted``
    to both, and the grounded case is what catches it. That inference is the
    conflation ADR-018 rules out.
    """

    return ChatTurnResult(
        outcome=_outcome(),
        answer=ANSWER,
        authorized_revisions=(),
        citations=(),
        grounded=grounded,
    )


async def _release(
    harness: ChatReleaseHarness,
    result: ChatTurnResult,
) -> tuple[StoredChatTurn, list[str]]:
    """Prepare one candidate, publish it, and report what the log now says."""

    await harness.conversations.create_session(
        session_id=SESSION,
        tenant_id=TENANT,
        owner_id=OWNER,
    )
    claim = await harness.conversations.claim_turn(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=KEY,
        request_hash=REQUEST_HASH,
        run_id=RUN,
        user_message=user_message("a question"),
        lease_seconds=LEASE_SECONDS,
    )
    pending = await harness.conversations.prepare_release(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        turn_id=claim.turn.turn_id,
        result=result,
    )
    released = await harness.coordinator.release(
        turn=pending,
        tenant_id=TENANT,
        principal_id=OWNER,
        stream_id=SESSION,
        run_id=RUN,
        refusal_text=REFUSAL,
        sink=harness.sink(SCOPE),
    )
    events = await harness.events.read(SESSION, limit=500)
    return released, [
        envelope.payload.kind
        for envelope in events
        if envelope.payload.kind in ANSWER_KINDS
    ]


def test_a_grounded_release_commits_a_grounded_answer_event(
    chat_release: StoreHarness,
) -> None:
    """The control for the case below.

    Without it, a coordinator that wrote ``UngroundedAnswerCommitted`` for
    every turn would pass the ungrounded assertion while marking every verified
    answer in the audit log as unverified.
    """

    async def scenario(harness: ChatReleaseHarness) -> tuple[Any, list[str]]:
        released, kinds = await _release(harness, _candidate(grounded=True))
        return (released.status, released.result), kinds

    (status, stored), kinds = chat_release.run(scenario)

    assert kinds == ["AnswerCommitted"]
    assert status == "committed"
    assert stored is not None and stored.grounded is True


def test_an_ungrounded_release_commits_its_own_event(
    chat_release: StoreHarness,
) -> None:
    """The ADR-018 distinction, asserted where it becomes durable.

    ``grounded`` is read rather than inferred from an empty citation list,
    because "retrieved and cited nothing" and "never retrieved" are the two
    states the separate event exists to keep apart, and both look identical
    from the citations alone.
    """

    async def scenario(harness: ChatReleaseHarness) -> tuple[Any, list[str]]:
        released, kinds = await _release(harness, _candidate(grounded=False))
        return (released.status, released.result), kinds

    (status, stored), kinds = chat_release.run(scenario)

    assert kinds == ["UngroundedAnswerCommitted"]
    # Committed, not withheld: an unverified answer is still published. What
    # changes is what the log claims about it.
    assert status == "committed"
    assert stored is not None and stored.grounded is False


def test_a_withheld_replacement_keeps_the_label_of_the_candidate_it_replaces() -> None:
    """Both scrubbing helpers, asserted directly rather than through `release`.

    No public path reaches them with an ungrounded candidate today: an
    ungrounded result may not carry authorized revisions, and a revision guard
    short-circuits to True on an empty tuple, so the one withhold trigger that
    exists cannot fire for one. The pass-through is what keeps the label honest
    the first time a trigger appears that does not depend on revisions --
    `grounded` defaults to True, is written back with the replacement, and is
    read straight off the stored result by the API.
    """

    ungrounded = _candidate(grounded=False)
    replacements = (
        _withheld(ungrounded, REFUSAL),
        PostgresChatReleaseCoordinator._withheld_result(
            ungrounded,
            refusal_text=REFUSAL,
        ),
    )

    for replacement in replacements:
        assert replacement.withheld is True
        assert replacement.answer == REFUSAL
        assert replacement.citations == ()
        assert replacement.authorized_revisions == ()
        assert replacement.grounded is False

    # And a grounded candidate keeps its own label, so the field is carried
    # rather than pinned to either constant.
    grounded = _candidate(grounded=True)
    assert _withheld(grounded, REFUSAL).grounded is True
    assert (
        PostgresChatReleaseCoordinator._withheld_result(
            grounded,
            refusal_text=REFUSAL,
        ).grounded
        is True
    )


CITED = Citation(chunk_id="chunk_0001", document_id="doc_0001", document_version="1")


def _cited_candidate() -> ChatTurnResult:
    """A grounded result that actually names a source."""

    return ChatTurnResult(
        outcome=_outcome(),
        answer=ANSWER,
        authorized_revisions=(
            AuthorizedRevision(document_id="doc_0001", source_revision=1),
        ),
        citations=(CITED,),
        grounded=True,
    )


async def _prepare(harness: ChatReleaseHarness, result: ChatTurnResult) -> str:
    """Claim one turn and stage ``result`` on it; return the turn id.

    Stops short of the coordinator on purpose. The coordinator's fence re-reads
    every authorized revision from the document store, and ``doc_0001`` exists
    in no test database -- so on PostgreSQL it would (correctly) withhold this
    very answer, and the projection under test would never see a committed
    turn. What these tests pin is the *projection* of a committed turn, so
    they commit it the way the fence does once it has passed: through the
    store's own ``mark_released``.
    """

    await harness.conversations.create_session(
        session_id=SESSION,
        tenant_id=TENANT,
        owner_id=OWNER,
    )
    claim = await harness.conversations.claim_turn(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=KEY,
        request_hash=REQUEST_HASH,
        run_id=RUN,
        user_message=user_message("a question"),
        lease_seconds=LEASE_SECONDS,
    )
    await harness.conversations.prepare_release(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        turn_id=claim.turn.turn_id,
        result=result,
    )
    return claim.turn.turn_id


def test_history_carries_the_released_turns_evidence(
    chat_release: StoreHarness,
) -> None:
    """After a release, ``history()`` says which turn each answer was and what it cited.

    The console rebuilds a reloaded conversation from this projection, and
    until 2026-09-05 it carried role, text and usage only -- so every citation
    vanished on refresh (review 2026-09-04, item A). Three facts are pinned:
    the assistant message names its turn (the id the passage route is mounted
    under, so a citation stays a re-authorised read rather than a cached
    text), it carries the citations the release published, and it says the
    answer was grounded. The user's own message has none of them.
    """

    async def scenario(harness: ChatReleaseHarness) -> None:
        turn_id = await _prepare(harness, _cited_candidate())
        released = await harness.conversations.mark_released(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, turn_id=turn_id
        )
        assert released.status == "committed"
        history = await harness.conversations.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        asked, answered = history
        assert asked.message.role == "user"
        assert asked.turn_id is None
        assert asked.citations == ()
        assert asked.grounded is None
        assert answered.message.role == "assistant"
        assert answered.turn_id == turn_id
        assert answered.citations == (CITED,)
        assert answered.grounded is True

    chat_release.run(scenario)


def test_history_says_nothing_about_evidence_for_a_withheld_answer(
    chat_release: StoreHarness,
) -> None:
    """A withheld turn is linked to its message but hands on no evidence.

    Its stored result is a scrubbed shell by construction, so there is nothing
    to give -- and ``grounded`` is ``None`` rather than ``False``: ``False`` is
    the "answered without retrieving" fact the console warns about, and a
    withheld answer earned no such warning.
    """

    async def scenario(harness: ChatReleaseHarness) -> None:
        candidate = _cited_candidate()
        turn_id = await _prepare(harness, candidate)
        released = await harness.conversations.mark_released(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=turn_id,
            withheld_result=_withheld(candidate, REFUSAL),
        )
        assert released.status == "withheld"
        history = await harness.conversations.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        _asked, answered = history
        assert answered.turn_id == turn_id
        assert answered.citations == ()
        assert answered.grounded is None

    chat_release.run(scenario)
