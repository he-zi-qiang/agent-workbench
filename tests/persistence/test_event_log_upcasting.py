"""Old rows are translated; unreadable rows are isolated and counted.

Two failures share a symptom -- a stored row this process cannot turn into an
envelope -- and must not share an answer. A row written by an older envelope
contract is understood one version late, so an upcaster raises it. A damaged
row is understood not at all, so replay may skip it, but only out loud: the
whole point of the isolating path is that a caller can tell a partial replay
from a complete one.

Everything that goes through a row needs real PostgreSQL: the corruption under
test *is* a stored row, and a fake that returns whatever it was handed cannot
have one. Each such scenario writes to a stream id of its own and never
truncates -- other suites share this database and truncate ``events`` freely, a
TRUNCATE here would delete their rows just as readily, and this suite only ever
reads back the stream it just wrote.

The chain across versions is tested against the registry alone, with no
database at all. While ``DOMAIN_SCHEMA_VERSION`` is 1 there is no stored row
whose chain is longer than a single step, so the composition the class exists
for cannot be reached from a row; those scenarios raise the version the module
reads and register steps under it instead. Nothing they touch is storable, and
that is the point -- the question is what the loop does between two versions,
not what a row looks like.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence import event_log as event_log_module
from agent_workbench.adapters.persistence.event_log import (
    EventUpcaster,
    EventUpcasterRegistry,
    PostgresEventLog,
    StoredEnvelope,
)
from agent_workbench.adapters.persistence.models import events
from agent_workbench.domain.events import RunCompleted, RunStarted, ToolStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION
from agent_workbench.ports.event_log import EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
REQUIRED_TEST_DATABASE_SUFFIX = "_test"

BUDGET = RunBudget(max_steps=4, max_tool_calls=4)

# The version an upcaster can legally be registered for while the current one
# is 1: "written before envelopes carried a version". Named rather than spelled
# 0 everywhere, so the day DOMAIN_SCHEMA_VERSION moves it is one edit.
LEGACY_VERSION = DOMAIN_SCHEMA_VERSION - 1


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    database = dsn.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    if not database.endswith(REQUIRED_TEST_DATABASE_SUFFIX):
        raise AssertionError(
            f"{TEST_DSN_ENV_VAR} must name a database ending in "
            f"{REQUIRED_TEST_DATABASE_SUFFIX!r}; this suite writes damaged rows"
        )
    return dsn


def _run[T](scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    dsn = _dsn()

    async def execute() -> T:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _scope() -> EventScope:
    # Unique per scenario: see the module docstring on why nothing truncates.
    return EventScope(
        stream_id=f"str_upcast_{uuid.uuid4().hex[:16]}",
        run_id="run_upcast",
    )


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


async def _write_three(log: PostgresEventLog, scope: EventScope) -> tuple[str, ...]:
    """One ordinary stream: three durable events at sequences 1, 2, 3."""

    first = await log.append(scope, _started())
    second = await log.append(
        scope, ToolStarted(tool_call_id="tc_1", tool_name="knowledge_search")
    )
    third = await log.append(scope, RunCompleted(stop_reason="completed"))
    return (first.event_id, second.event_id, third.event_id)


async def _damage(engine: AsyncEngine, event_id: str, **values: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(events).where(events.c.event_id == event_id).values(**values)
        )


def _legacy_run_started_payload() -> dict[str, Any]:
    """What a ``RunStarted`` would look like if the field had another name.

    Derived from the current payload rather than written out, so this stays a
    rename of one field and does not quietly become a second, stale copy of
    the whole event shape.
    """

    payload = _started().model_dump(mode="json")
    payload["profile"] = payload.pop("model_profile")
    return payload


def _rename_profile(stored: StoredEnvelope) -> StoredEnvelope:
    """The test-only upcaster: ``profile`` was renamed to ``model_profile``.

    Note what it does not do: it never touches ``schema_version``. The registry
    owns the bump, and the assertions below rely on that -- the envelope comes
    back at the current version even though nothing here set it.
    """

    raised = dict(stored)
    payload = dict(cast(dict[str, Any], raised["payload"]))
    payload["model_profile"] = payload.pop("profile")
    raised["payload"] = payload
    return raised


def _registry_with_rename() -> EventUpcasterRegistry:
    registry = EventUpcasterRegistry()
    registry.register("RunStarted", LEGACY_VERSION, _rename_profile)
    return registry


def test_a_registered_upcaster_raises_an_older_row_to_the_current_contract() -> None:
    """The stored row is only readable *through* the upcaster.

    The version alone would be a weak assertion -- bumping a number proves
    nothing. The payload is stored under the old field name too, so an
    envelope that comes back at all is one the registered function produced.
    """

    async def scenario(engine: AsyncEngine) -> tuple[int, str, int]:
        scope = _scope()
        log = PostgresEventLog(engine)
        written = await log.append(scope, _started())
        await _damage(
            engine,
            written.event_id,
            schema_version=LEGACY_VERSION,
            payload=_legacy_run_started_payload(),
        )

        upcasting = PostgresEventLog(engine, upcasters=_registry_with_rename())
        replayed = (await upcasting.read(scope.stream_id))[0]
        assert isinstance(replayed.payload, RunStarted)
        return (
            replayed.schema_version,
            replayed.payload.model_profile,
            replayed.sequence or 0,
        )

    assert _run(scenario) == (DOMAIN_SCHEMA_VERSION, "main", 1)


def test_an_older_row_without_an_upcaster_is_still_refused() -> None:
    """The control: the log ships with an empty registry, so nothing changes."""

    async def scenario(engine: AsyncEngine) -> None:
        scope = _scope()
        log = PostgresEventLog(engine)
        written = await log.append(scope, _started())
        await _damage(
            engine,
            written.event_id,
            schema_version=LEGACY_VERSION,
            payload=_legacy_run_started_payload(),
        )
        await log.read(scope.stream_id)

    with pytest.raises(ValidationError, match="schema_version"):
        _run(scenario)


def test_an_upcaster_for_the_current_version_is_refused() -> None:
    """A step that does not move the envelope would loop or lie."""

    with pytest.raises(ValueError, match="below the current domain schema version"):
        EventUpcasterRegistry().register(
            "RunStarted", DOMAIN_SCHEMA_VERSION, _rename_profile
        )


def test_a_second_upcaster_for_one_step_is_refused() -> None:
    """Import order must not get to decide which migration ran."""

    registry = _registry_with_rename()
    with pytest.raises(ValueError, match="already registered"):
        registry.register("RunStarted", LEGACY_VERSION, _rename_profile)


# --- The chain across versions ----------------------------------------------
#
# Every scenario above stores a row, and a stored row's chain is one step long
# while `DOMAIN_SCHEMA_VERSION` is 1. An implementation that applied its first
# step and stopped -- or that decided once, before the loop, which event type
# to look steps up under -- passes all of them. Those are the two claims the
# registry's docstring makes for choosing single-version steps over upcasters
# that jump straight to the current version, so they are exactly what must not
# rest on a version number that has never moved.
#
# Raising the version the module reads is the smallest change that makes a
# longer chain reachable: `register` then admits steps for versions this
# contract has never had, and the loop has somewhere to walk. It is patched on
# the module rather than on `domain.schema` because that is the binding both
# `register` and `raise_to_current` resolve.


def _marks(step: str, *, rename_to: str | None = None) -> EventUpcaster:
    """A step that records having run, and optionally renames the event type.

    The mark is appended to a list rather than set as a flag so the *order* of
    the steps survives into the assertion. A chain that applied the right steps
    in the wrong sequence is a different defect from one that skipped a step,
    and a flag per step could not tell the two apart.

    Like the rename upcaster above, it never touches ``schema_version``: the
    registry owns the bump, and every version asserted below was set by the
    loop rather than by anything registered into it.
    """

    def upcaster(stored: StoredEnvelope) -> StoredEnvelope:
        raised = dict(stored)
        applied = list(cast(list[str], raised.get("applied", [])))
        applied.append(step)
        raised["applied"] = applied
        if rename_to is not None:
            raised["event_type"] = rename_to
        return raised

    return upcaster


def test_the_chain_applies_one_step_per_version_up_to_the_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three registered steps, three applications, one final version.

    Equality against the whole envelope rather than a field of it, because
    every way this can go wrong is a different envelope: a skipped step, a step
    applied twice, steps out of order, or a chain that stopped short of the
    current version. The implementation this repository could have shipped
    instead -- one that raises an envelope a single step -- returns
    ``["v1"]`` at version 1 here.
    """

    monkeypatch.setattr(event_log_module, "DOMAIN_SCHEMA_VERSION", 3)
    registry = EventUpcasterRegistry()
    registry.register("RunLaunched", 0, _marks("v1", rename_to="RunStarted"))
    registry.register("RunStarted", 1, _marks("v2"))
    registry.register("RunStarted", 2, _marks("v3"))

    raised = registry.raise_to_current(
        {"event_type": "RunLaunched", "schema_version": 0},
        from_version=0,
    )

    assert dict(raised) == {
        "event_type": "RunStarted",
        "schema_version": 3,
        "applied": ["v1", "v2", "v3"],
    }


def test_each_step_is_found_under_the_event_type_the_previous_one_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renaming an event type is one of the migrations the chain exists for.

    Both candidates for the second step are registered -- one under the name
    the row was stored with, one under the name the rename produced -- so what
    comes back distinguishes "the loop read the event type again" from "the
    loop kept the name it started with". Only the second lookup can be right:
    after the rename there is no ``RunLaunched`` envelope left for a
    ``RunLaunched`` upcaster to receive, so a chain that found that step would
    hand a renamed envelope to a function written for the old shape.
    """

    monkeypatch.setattr(event_log_module, "DOMAIN_SCHEMA_VERSION", 2)
    registry = EventUpcasterRegistry()
    registry.register("RunLaunched", 0, _marks("renamed", rename_to="RunStarted"))
    registry.register("RunStarted", 1, _marks("under the new name"))
    registry.register("RunLaunched", 1, _marks("under the old name"))

    raised = registry.raise_to_current(
        {"event_type": "RunLaunched", "schema_version": 0},
        from_version=0,
    )

    assert dict(raised) == {
        "event_type": "RunStarted",
        "schema_version": 2,
        "applied": ["renamed", "under the new name"],
    }


def test_a_chain_with_a_hole_stops_there_and_stays_labelled_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing middle step is refused by validation, not stepped over.

    The step for the version *after* the hole is registered, and must not run:
    reaching it would mean an envelope of one shape was handed to a function
    written for another, which is worse than not migrating at all. Stopping
    leaves ``schema_version`` one short of the current one, and that is what
    makes ``EventEnvelope`` refuse it -- the same single refusal path an
    unknown version had before upcasting existed.
    """

    monkeypatch.setattr(event_log_module, "DOMAIN_SCHEMA_VERSION", 3)
    registry = EventUpcasterRegistry()
    registry.register("RunStarted", 0, _marks("v1"))
    # Nothing for version 1: the release that introduced v2 forgot its step.
    registry.register("RunStarted", 2, _marks("v3"))

    raised = registry.raise_to_current(
        {"event_type": "RunStarted", "schema_version": 0},
        from_version=0,
    )

    assert dict(raised) == {
        "event_type": "RunStarted",
        "schema_version": 1,
        "applied": ["v1"],
    }


def test_a_step_that_drops_the_event_type_stops_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discriminator can only go missing mid-chain, so only a chain finds it.

    A stored row always has an event type -- it is a column -- so the guard in
    the loop is unreachable on the first round. It exists for the round after a
    step that dropped the field, and what it does is return what the chain has
    reached rather than raise a ``KeyError`` from inside the loop, so the
    missing discriminator is named by envelope validation like any other
    malformed envelope.
    """

    def _drops_the_event_type(stored: StoredEnvelope) -> StoredEnvelope:
        raised = dict(stored)
        del raised["event_type"]
        return raised

    monkeypatch.setattr(event_log_module, "DOMAIN_SCHEMA_VERSION", 3)
    registry = EventUpcasterRegistry()
    registry.register("RunStarted", 0, _drops_the_event_type)
    registry.register("RunStarted", 1, _marks("never runs"))

    raised = registry.raise_to_current(
        {"event_type": "RunStarted", "schema_version": 0},
        from_version=0,
    )

    assert dict(raised) == {"schema_version": 1}


def test_replay_isolates_a_poison_row_and_delivers_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One damaged row in the middle does not take the stream with it."""

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        # A payload that lost every field but its discriminator: the shape a
        # partially written row or a bad hand-edit leaves behind.
        await _damage(engine, second, payload={"kind": "RunStarted"})

        page = await log.read_isolating(scope.stream_id)
        return (
            [envelope.sequence for envelope in page.events],
            page.skipped,
            page.quarantined,
            page.resume_after,
            scope.stream_id,
            second,
        )

    with caplog.at_level(
        logging.ERROR, logger="agent_workbench.adapters.persistence.event_log"
    ):
        sequences, skipped, quarantined, resume, stream_id, damaged = _run(scenario)

    # The readable events either side of the damage are delivered, and the
    # caller is told, in a value it cannot miss, that one row was not.
    assert (sequences, skipped, resume) == ([1, 3], 1, 3)
    assert len(quarantined) == 1
    record = quarantined[0]
    assert (record.stream_id, record.sequence, record.event_id) == (
        stream_id,
        2,
        damaged,
    )
    # The reason names the fields that failed, and never their values.
    assert "run_kind" in record.reason

    # And an operator can find the row from the log alone, which needs the id
    # and the position in it -- "a row was skipped" would be unactionable.
    logged = [line.getMessage() for line in caplog.records]
    assert any(
        damaged in message and f"sequence {record.sequence}" in message
        for message in logged
    ), f"the skip was not logged in a findable form: {logged}"


def test_a_poison_row_still_stops_the_strict_read() -> None:
    """Isolating is opt-in. ``read`` is the contract every caller has today."""

    async def scenario(engine: AsyncEngine) -> None:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        await _damage(engine, second, payload={"kind": "RunStarted"})
        await log.read(scope.stream_id)

    with pytest.raises(ValidationError):
        _run(scenario)


def test_a_row_from_an_unknown_newer_contract_is_isolated_too() -> None:
    """Not only damage. A version nothing can lower is equally undeliverable."""

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        await _damage(engine, second, schema_version=DOMAIN_SCHEMA_VERSION + 1)

        page = await log.read_isolating(scope.stream_id)
        return (
            [envelope.sequence for envelope in page.events],
            page.skipped,
            page.quarantined[0].schema_version,
            "unsupported domain schema version" in page.quarantined[0].reason,
        )

    assert _run(scenario) == ([1, 3], 1, DOMAIN_SCHEMA_VERSION + 1, True)


def test_a_trailing_poison_row_advances_the_resume_cursor_past_itself() -> None:
    """The case a cursor taken from the delivered events would stall on.

    Both replay loops in this repository resume from the last event they
    received. If the poison row is last, that cursor sits *before* it, and the
    next page reads the same unreadable row forever. ``resume_after`` is the
    highest position examined, so the follow-up page is empty and the skip is
    already accounted for in the first page's ``quarantined``.
    """

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, _, third = await _write_three(log, scope)
        await _damage(engine, third, payload={"kind": "RunStarted"})

        first_page = await log.read_isolating(scope.stream_id)
        follow_up = await log.read_isolating(
            scope.stream_id, after_sequence=first_page.resume_after
        )
        return (
            [envelope.sequence for envelope in first_page.events],
            first_page.skipped,
            first_page.resume_after,
            [envelope.sequence for envelope in follow_up.events],
            follow_up.skipped,
            follow_up.resume_after,
        )

    assert _run(scenario) == ([1, 2], 1, 3, [], 0, None)


def test_a_clean_stream_replays_identically_through_both_paths() -> None:
    """The control group: with nothing to skip, nothing is skipped.

    An isolating replay that quietly dropped a readable event would still
    "work" in every test above. This is the one that would catch it.
    """

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        await _write_three(log, scope)

        strict = await log.read(scope.stream_id)
        page = await log.read_isolating(scope.stream_id)
        return (
            page.events == strict,
            len(page.events),
            page.quarantined,
            page.skipped,
            page.resume_after,
        )

    assert _run(scenario) == (True, 3, (), 0, 3)


def test_an_empty_page_reports_no_position_to_resume_from() -> None:
    """Nothing examined means the caller's own cursor is still the truth."""

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        await _write_three(log, scope)
        page = await log.read_isolating(scope.stream_id, after_sequence=3)
        return (page.events, page.quarantined, page.resume_after)

    assert _run(scenario) == ((), (), None)


def test_the_isolating_read_is_bounded_like_the_strict_one() -> None:
    """Same page bounds, or a client could ask for a stream by another name."""

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        await _write_three(log, scope)
        page = await log.read_isolating(scope.stream_id, limit=2)
        with pytest.raises(ValueError, match="limit must be positive"):
            await log.read_isolating(scope.stream_id, limit=0)
        return ([envelope.sequence for envelope in page.events], page.resume_after)

    assert _run(scenario) == ([1, 2], 2)


def test_the_damaged_rows_are_left_in_place() -> None:
    """Isolation is not deletion: the row survives for someone to repair."""

    async def scenario(engine: AsyncEngine) -> int:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        await _damage(engine, second, payload={"kind": "RunStarted"})
        await log.read_isolating(scope.stream_id)

        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    text("SELECT count(*) FROM events WHERE stream_id = :stream"),
                    {"stream": scope.stream_id},
                )
            ).scalar_one()
        return int(stored)

    assert _run(scenario) == 3
