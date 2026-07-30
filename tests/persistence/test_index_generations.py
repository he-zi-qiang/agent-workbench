"""Retiring, releasing and collecting an index generation, in that order.

The ordering is the invariant. A collection may only disappear when the index is
retired and nothing holds it, and only a *finished* Task may let go -- so no
sequence of these three can take an index away from a Task that is still going
to read from it.

Real PostgreSQL only: what is being tested is which rows the database refuses.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text

from agent_workbench.adapters.persistence import (
    PostgresIndexGenerationStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import qdrant_index_generations
from agent_workbench.ports.index_generations import (
    GenerationStillReferencedError,
    IndexGenerationStore,
)
from agent_workbench.ports.task_registry import (
    IndexGenerationNotReservableError,
    IndexReservation,
    TaskSubmission,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

GENERATION = "6f1d5a02-0000-4000-8000-0000000000aa"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(
    scenario: Callable[
        [PostgresTaskRegistry, PostgresIndexGenerationStore], Awaitable[Any]
    ],
) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE task_runs, events, event_streams, "
                        "qdrant_index_generations CASCADE"
                    )
                )
            return await scenario(
                PostgresTaskRegistry(engine), PostgresIndexGenerationStore(engine)
            )
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
        "submitted_principal_scopes": [],
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _generation(
    store: Any, engine: Any, *, status: str = "active"
) -> IndexReservation:
    async with engine.begin() as connection:
        await connection.execute(
            qdrant_index_generations.insert().values(
                generation_id=GENERATION,
                collection_name="kb_v3",
                index_version="3",
                status=status,
            )
        )
    return IndexReservation(
        collection_name="kb_v3", index_version="3", generation_id=GENERATION
    )


async def _remaining(engine: Any) -> int:
    async with engine.connect() as connection:
        return len((await connection.execute(select(qdrant_index_generations))).all())


# --------------------------------------------------------------------------


def test_the_store_satisfies_the_framework_neutral_port() -> None:
    dsn = _dsn()
    engine = create_query_engine(dsn, application_name="agent-workbench-tests")
    try:
        assert isinstance(PostgresIndexGenerationStore(engine), IndexGenerationStore)
    finally:
        asyncio.run(engine.dispose())


def test_retiring_stops_new_reservations_without_touching_existing_ones() -> None:
    """The difference between retiring an index and deleting it.

    A Task that already reserved the generation keeps it; the next submission is
    refused. That is what lets an alias switch drain instead of cut.
    """

    async def scenario(registry: Any, store: Any) -> tuple[Any, ...]:
        engine = registry._engine
        reservation = await _generation(store, engine)
        held = await registry.submit(_submission(index_reservation=reservation))
        await store.retire(GENERATION)
        with pytest.raises(IndexGenerationNotReservableError):
            await registry.submit(
                _submission(
                    thread_id="thr_2",
                    submission_dedup_key="dedup_2",
                    index_reservation=reservation,
                )
            )
        still = await registry.get(held.task_id)
        assert still is not None
        return still.resolved_qdrant_index_generation_id, await _remaining(engine)

    assert _run(scenario) == (GENERATION, 1)


def test_a_running_task_refuses_to_release_its_index() -> None:
    """The one mistake here that must be refused rather than reported.

    A reservation is what keeps the index alive for the rest of the run.
    Releasing it early would let the collector take the corpus away from a Task
    that is still reading it.
    """

    async def scenario(registry: Any, store: Any) -> tuple[bool, Any]:
        engine = registry._engine
        reservation = await _generation(store, engine)
        opened = await registry.submit(_submission(index_reservation=reservation))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        released = await store.release(opened.task_id)
        still = await registry.get(opened.task_id)
        assert still is not None
        return released, still.resolved_qdrant_index_generation_id

    assert _run(scenario) == (False, GENERATION)


def test_a_finished_task_releases_its_index_but_keeps_its_snapshot() -> None:
    """What is given up is the reservation, not the record of what ran.

    Which concrete index the Task used stays in its semantics snapshot, so the
    audit does not depend on the generation row surviving.
    """

    async def scenario(registry: Any, store: Any) -> tuple[Any, ...]:
        engine = registry._engine
        reservation = await _generation(store, engine)
        opened = await registry.submit(_submission(index_reservation=reservation))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.mark_succeeded(claim.lease)
        released = await store.release(opened.task_id)
        settled = await registry.get(opened.task_id)
        assert settled is not None
        return (
            released,
            settled.resolved_qdrant_index_generation_id,
            settled.resolved_qdrant_collection,
            settled.run_semantics_revision,
        )

    released, generation, collection, revision = _run(scenario)

    assert released is True
    # The hold is gone, all three columns together.
    assert generation is None
    assert collection is None
    # And the Task still knows what it ran under.
    assert revision == "1.2:v1.3:abc0123456789def"


def test_releasing_twice_reports_that_there_was_nothing_to_release() -> None:
    async def scenario(registry: Any, store: Any) -> tuple[bool, bool]:
        engine = registry._engine
        reservation = await _generation(store, engine)
        opened = await registry.submit(_submission(index_reservation=reservation))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.mark_succeeded(claim.lease)
        return await store.release(opened.task_id), await store.release(opened.task_id)

    assert _run(scenario) == (True, False)


def test_a_generation_a_task_still_holds_is_not_collected() -> None:
    """Task-aware: the collector counts holders, whatever the alias now says."""

    async def scenario(registry: Any, store: Any) -> tuple[int, int]:
        engine = registry._engine
        reservation = await _generation(store, engine)
        await registry.submit(_submission(index_reservation=reservation))
        await store.retire(GENERATION)
        with pytest.raises(GenerationStillReferencedError) as captured:
            await store.collect(GENERATION)
        return captured.value.references, await _remaining(engine)

    references, remaining = _run(scenario)

    assert references == 1
    assert remaining == 1


def test_an_active_generation_is_not_collected_even_with_no_holders() -> None:
    """Retirement first. Deleting the index new requests are routed to is not GC."""

    async def scenario(registry: Any, store: Any) -> tuple[int, int]:
        engine = registry._engine
        await _generation(store, engine)
        with pytest.raises(GenerationStillReferencedError) as captured:
            await store.collect(GENERATION)
        return captured.value.references, await _remaining(engine)

    assert _run(scenario) == (0, 1)


def test_a_retired_generation_nothing_holds_is_collected() -> None:
    """The whole sequence: retire, finish, release, collect."""

    async def scenario(registry: Any, store: Any) -> int:
        engine = registry._engine
        reservation = await _generation(store, engine)
        opened = await registry.submit(_submission(index_reservation=reservation))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.mark_succeeded(claim.lease)
        await store.retire(GENERATION)
        assert await store.release(opened.task_id) is True
        await store.collect(GENERATION)
        return await _remaining(engine)

    assert _run(scenario) == 0


def test_collecting_something_already_gone_is_not_an_error() -> None:
    """A sweep that runs twice is a sweep, not a fault."""

    async def scenario(registry: Any, store: Any) -> int:
        engine = registry._engine
        await _generation(store, engine, status="retired")
        await store.collect(GENERATION)
        await store.collect(GENERATION)
        return await _remaining(engine)

    assert _run(scenario) == 0


def test_a_submission_racing_a_collection_cannot_leave_a_dangling_hold() -> None:
    """Both orderings stay consistent, because both lock the generation row.

    The collector holds the row while it counts; a submission arriving mid-count
    waits, then finds the generation gone or retired and fails closed. What must
    never happen is a committed Task pointing at a collected generation.
    """

    async def scenario(registry: Any, store: Any) -> tuple[str, int, int]:
        engine = registry._engine
        reservation = await _generation(store, engine, status="retired")

        holder = await engine.connect()
        transaction = await holder.begin()
        await holder.execute(
            select(qdrant_index_generations.c.status)
            .where(qdrant_index_generations.c.generation_id == GENERATION)
            .with_for_update()
        )
        submitting = asyncio.create_task(_submit_or_error(registry, reservation))
        await asyncio.sleep(0.3)
        await transaction.commit()
        await holder.close()
        outcome = await submitting

        async with engine.connect() as connection:
            tasks = len(
                (await connection.execute(select(qdrant_index_generations))).all()
            )
        return type(outcome).__name__, tasks, await _remaining(engine)

    error, generations, remaining = _run(scenario)

    assert error == "IndexGenerationNotReservableError"
    assert generations == remaining == 1


def test_a_collection_racing_an_in_flight_submission_reports_the_holder() -> None:
    """Why `collect` locks the row before it counts.

    A submission in flight has already locked the generation and inserted its
    Task, uncommitted. A collector that counted without taking the lock would
    see zero holders, decide the generation is free, and then have its DELETE
    refused by the foreign key -- safe, but reported as a constraint violation
    from two layers down instead of as "one task still holds this". Taking the
    lock makes the collector wait and then say what is true.

    Modelled by holding exactly the state a submission holds mid-transaction,
    because pausing the real one would need a seam that exists only for a test.
    """

    async def scenario(registry: Any, store: Any) -> tuple[str, int]:
        engine = registry._engine
        await _generation(store, engine, status="retired")

        holder = await engine.connect()
        transaction = await holder.begin()
        # Exactly what a submission does: lock the generation, then insert the
        # Task that references it.
        await holder.execute(
            select(qdrant_index_generations.c.status)
            .where(qdrant_index_generations.c.generation_id == GENERATION)
            .with_for_update()
        )
        await holder.execute(
            text(
                "INSERT INTO task_runs (task_id, tenant_id, owner_id, thread_id,"
                " graph_version, input_ref, input_fingerprint,"
                " submission_dedup_key, run_semantics_snapshot,"
                " run_semantics_revision, submitted_policy_revision,"
                " submitted_policy_fingerprint, submitted_authorization_envelope,"
                " submitted_principal_scopes, resolved_qdrant_collection,"
                " resolved_qdrant_index_version,"
                " resolved_qdrant_index_generation_id, status)"
                " VALUES ('task_race', 'tenant_a', 'user_1', 'thr_race', 'v1',"
                " 'input_1', :digest, 'dedup_race', '{}'::jsonb, 'r', 'p', 'f',"
                " '{}'::jsonb, '[]'::jsonb, 'kb_v3', '3', :generation, 'queued')"
            ),
            {
                "digest": hashlib.sha256(b"input_1").hexdigest(),
                "generation": GENERATION,
            },
        )
        collecting = asyncio.create_task(_collect_or_error(store))
        await asyncio.sleep(0.3)
        await transaction.commit()
        await holder.close()
        outcome = await collecting
        return type(outcome).__name__, await _remaining(engine)

    error, remaining = _run(scenario)

    # The holder is reported as a holder, not as a foreign key.
    assert error == "GenerationStillReferencedError"
    assert remaining == 1


async def _collect_or_error(store: Any) -> object:
    try:
        return await store.collect(GENERATION)
    except Exception as error:
        return error


async def _submit_or_error(registry: Any, reservation: IndexReservation) -> object:
    try:
        return await registry.submit(_submission(index_reservation=reservation))
    except IndexGenerationNotReservableError as error:
        return error
