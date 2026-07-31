"""The ledger that stands between a retry and a second real effect.

Three properties are what this table exists for, and each has its control group:

* one operation key is one request. The same arguments replay; different
  arguments under the same key are refused rather than recorded.
* a succeeded operation says so to whoever asks next, so a retry declines to
  dispatch instead of dispatching again.
* the unknown window is a state, not a guess. A process that dies between the
  dispatch and the report leaves ``intended``; a caller that knows it does not
  know writes ``needs_reconciliation``.

And the whole thing is fenced on the Task's live lease, because an effect
dispatched by a Worker that lost the Task is an effect the Worker that replaced
it will dispatch again.

Real PostgreSQL only: every one of these is a conditional write.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any, get_args

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from agent_workbench.adapters.persistence import (
    PostgresTaskRegistry,
    PostgresToolExecutionLedger,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import tool_executions
from agent_workbench.domain.tools import argument_digest
from agent_workbench.ports.task_registry import TaskSubmission
from agent_workbench.ports.tool_executions import (
    ToolExecutionIntent,
    ToolExecutionLedger,
    ToolExecutionNotWritableError,
    ToolExecutionStatus,
    ToolOperationConflictError,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "tool_executions, approvals, task_runs, events, event_streams"

OPERATION = "export:report:v1"
REQUEST = {"artifact_id": "art_1", "destination": "reports/final.md"}


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(
    scenario: Callable[
        [Any, PostgresTaskRegistry, PostgresToolExecutionLedger], Awaitable[Any]
    ],
) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            return await scenario(
                engine,
                PostgresTaskRegistry(engine),
                PostgresToolExecutionLedger(engine),
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
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


def _intent(task_id: str, *, epoch: int = 1, **overrides: Any) -> ToolExecutionIntent:
    base: dict[str, Any] = {
        "task_id": task_id,
        "operation_key": OPERATION,
        "tool_name": "export_artifact",
        "canonical_request_hash": argument_digest(REQUEST),
        "lease_epoch": epoch,
        "agent_run_id": "run_1",
        "tool_call_id": "toolu_01",
        "policy_identity": "policy-1:ffffffffffffffff",
    }
    base.update(overrides)
    return ToolExecutionIntent.model_validate(base)


async def _claimed(registry: PostgresTaskRegistry) -> tuple[str, int]:
    """A Task with a live claim, which is the only state the ledger writes in."""

    task = await registry.submit(_submission())
    claim = await registry.claim_next("worker_1", lease_seconds=60)
    assert claim is not None
    return task.task_id, claim.lease.epoch


# --------------------------------------------------------------------------
# One key, one request
# --------------------------------------------------------------------------


def test_the_ledger_satisfies_the_framework_neutral_port() -> None:
    dsn = _dsn()
    engine = create_query_engine(dsn, application_name="agent-workbench-tests")
    try:
        assert isinstance(PostgresToolExecutionLedger(engine), ToolExecutionLedger)
    finally:
        asyncio.run(engine.dispose())


def test_recording_the_same_intent_twice_claims_one_operation() -> None:
    """A retried attempt asks about the operation it already opened."""

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        first = await ledger.record_intent(_intent(task_id, epoch=epoch))
        second = await ledger.record_intent(_intent(task_id, epoch=epoch))
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(tool_executions))).all())
        return first.execution_id, second.execution_id, rows, first.may_dispatch

    first, second, rows, may_dispatch = _run(scenario)

    assert first == second
    assert rows == 1
    assert may_dispatch is True


def test_one_key_with_different_arguments_is_refused_not_recorded() -> None:
    """The control group is the test above: same key, same arguments, fine.

    Here only the arguments differ. Returning the stored row would tell the
    caller an effect it never asked for had already been performed.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        with pytest.raises(ToolOperationConflictError) as captured:
            await ledger.record_intent(
                _intent(
                    task_id,
                    epoch=epoch,
                    canonical_request_hash=argument_digest(
                        {**REQUEST, "destination": "reports/other.md"}
                    ),
                )
            )
        async with engine.connect() as connection:
            rows = (await connection.execute(select(tool_executions))).mappings().all()
        return (
            captured.value.operation_key,
            len(rows),
            rows[0]["canonical_request_hash"],
        )

    operation_key, rows, stored_hash = _run(scenario)

    assert operation_key == OPERATION
    assert rows == 1
    # The first request stands. A conflict must not overwrite it.
    assert stored_hash == argument_digest(REQUEST)


def test_the_model_call_id_is_recorded_but_is_not_the_key() -> None:
    """A retried model turn mints a new call id for the same intent.

    If the call id were part of the key, that retry would be a second operation
    and therefore a second effect. The control group is the conflict test: what
    *does* distinguish operations is the canonical request.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        first = await ledger.record_intent(_intent(task_id, epoch=epoch))
        again = await ledger.record_intent(
            _intent(task_id, epoch=epoch, tool_call_id="toolu_99")
        )
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(tool_executions))).all())
        return first.execution_id, again.execution_id, again.tool_call_id, rows

    first, again, recorded_call_id, rows = _run(scenario)

    assert first == again
    assert rows == 1
    # The first attempt's id stays; the ledger is not a log of proposals.
    assert recorded_call_id == "toolu_01"


# --------------------------------------------------------------------------
# What a later attempt is told
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("succeeded", "expected_status"),
    [(True, "succeeded"), (False, "failed")],
)
def test_a_settled_operation_refuses_to_be_dispatched_again(
    succeeded: bool, expected_status: str
) -> None:
    """The whole point: a retry after a real effect must not repeat it.

    Both outcomes settle, and both make ``may_dispatch`` false -- a failure that
    the dispatch itself reported is knowledge, and re-running on it would be a
    retry policy this ledger deliberately does not own.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        settled = await ledger.record_result(
            task_id=task_id,
            operation_key=OPERATION,
            lease_epoch=epoch,
            succeeded=succeeded,
            detail="the provider answered",
        )
        # A later attempt asks the same question and is told no.
        retried = await ledger.record_intent(_intent(task_id, epoch=epoch))
        return (
            settled.status,
            settled.settled,
            retried.may_dispatch,
            retried.status,
            settled.settled_at is not None,
        )

    status, is_settled, may_dispatch, retried_status, has_timestamp = _run(scenario)

    assert status == expected_status
    assert is_settled is True
    assert may_dispatch is False
    assert retried_status == expected_status
    assert has_timestamp is True


def test_an_unknown_outcome_becomes_a_state_rather_than_a_guess() -> None:
    """The window nothing can close on its own.

    A caller that dispatched and cannot learn what happened says exactly that.
    The control group is the succeeded case above: this one is deliberately not
    reported as either outcome, and the reason is required rather than optional.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        parked = await ledger.mark_for_reconciliation(
            task_id=task_id,
            operation_key=OPERATION,
            lease_epoch=epoch,
            detail="the provider connection dropped after the request was sent",
        )
        retried = await ledger.record_intent(_intent(task_id, epoch=epoch))
        return parked.status, parked.outcome_detail, retried.may_dispatch

    status, detail, may_dispatch = _run(scenario)

    assert status == "needs_reconciliation"
    assert detail is not None
    # Not retried, and not written off. A human decides.
    assert may_dispatch is False


def test_an_operation_nobody_settled_stays_intended() -> None:
    """A dead process leaves a fact, not a verdict.

    This is the state the reconciliation status exists to be distinguishable
    from: nobody said the effect failed, and nobody said it landed.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        # No result is ever reported -- the process died here.
        stored = await ledger.get(task_id=task_id, operation_key=OPERATION)
        assert stored is not None
        return stored.status, stored.settled, stored.settled_at

    status, settled, settled_at = _run(scenario)

    assert (status, settled, settled_at) == ("intended", False, None)


# --------------------------------------------------------------------------
# The lease fence
# --------------------------------------------------------------------------


def test_a_worker_that_lost_the_task_cannot_record_an_intent() -> None:
    """The control group is inside the test: the live epoch works, the old one
    does not, and the difference is the only thing that changed."""

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        live = await ledger.record_intent(
            _intent(task_id, epoch=epoch, operation_key="export:first")
        )
        with pytest.raises(ToolExecutionNotWritableError) as captured:
            await ledger.record_intent(
                _intent(task_id, epoch=epoch + 1, operation_key="export:second")
            )
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(tool_executions))).all())
        return live.status, captured.value.found_lease_epoch, rows

    status, found_epoch, rows = _run(scenario)

    assert status == "intended"
    assert found_epoch == 1
    assert rows == 1


def test_an_expired_lease_cannot_dispatch_before_anybody_reclaims_it() -> None:
    """The window between a lease lapsing and the reaper noticing.

    The Task is still ``running`` at the same epoch -- nothing has moved it --
    so an epoch check alone would let this through. But the lease is over, and
    the reaper may hand the Task to another Worker at any moment; an effect
    dispatched here is one the next Worker will dispatch again.

    The control group is the first record below: the same call, the same epoch,
    with the lease still alive.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        alive = await ledger.record_intent(
            _intent(task_id, epoch=epoch, operation_key="export:while_leased")
        )

        # Only the clock moves. No reclaim, no status change, same epoch.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE task_runs SET lease_until = now() - interval '1 second'")
            )
            still_running = (
                await connection.execute(
                    text(
                        "SELECT status, lease_epoch FROM task_runs WHERE task_id = :t"
                    ),
                    {"t": task_id},
                )
            ).one()

        with pytest.raises(ToolExecutionNotWritableError) as opening:
            await ledger.record_intent(
                _intent(task_id, epoch=epoch, operation_key="export:after_expiry")
            )
        # Nor may it settle the intent it recorded while it still held the Task.
        with pytest.raises(ToolExecutionNotWritableError):
            await ledger.record_result(
                task_id=task_id,
                operation_key="export:while_leased",
                lease_epoch=epoch,
                succeeded=True,
            )
        stale = await ledger.get(task_id=task_id, operation_key="export:while_leased")
        assert stale is not None
        return (
            alive.status,
            tuple(still_running),
            opening.value.found_status,
            stale.status,
        )

    alive_status, task_row, found_status, stale_status = _run(scenario)

    assert alive_status == "intended"
    # Unchanged: the fence is about the lease, not about the status or epoch.
    assert task_row == ("running", 1)
    assert found_status == "running"
    assert stale_status == "intended"


def test_a_reclaimed_worker_cannot_report_the_result_of_its_own_intent() -> None:
    """The dangerous one.

    The first Worker dispatched, then lost the Task to a reclaim. Its result
    must not land: the Worker that replaced it is now responsible for the
    operation, and a stale report would settle a row the new attempt is about to
    read as its own answer.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        # The lease expires and the reaper hands the Task to somebody else.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE task_runs SET lease_until = now() - interval '1 minute'")
            )
        await registry.reclaim_expired(
            limit=10, max_attempts=5, retry_base_seconds=1, retry_max_seconds=60
        )
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE task_runs SET available_at = now()"))
        second = await registry.claim_next("worker_2", lease_seconds=60)
        assert second is not None

        with pytest.raises(ToolExecutionNotWritableError):
            await ledger.record_result(
                task_id=task_id,
                operation_key=OPERATION,
                lease_epoch=epoch,
                succeeded=True,
            )
        stale = await ledger.get(task_id=task_id, operation_key=OPERATION)
        assert stale is not None

        # And the new owner cannot settle an intent it did not record either:
        # what it may do is discover the operation and decide about it.
        with pytest.raises(ToolExecutionNotWritableError):
            await ledger.record_result(
                task_id=task_id,
                operation_key=OPERATION,
                lease_epoch=second.lease.epoch,
                succeeded=True,
            )
        return stale.status, second.lease.epoch

    status, new_epoch = _run(scenario)

    assert status == "intended"
    assert new_epoch == 2


def test_the_result_of_a_terminal_task_cannot_be_recorded() -> None:
    async def scenario(engine: Any, registry: Any, ledger: Any) -> Any:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        await registry.cancel(task_id, reason="the owner asked")
        with pytest.raises(ToolExecutionNotWritableError) as captured:
            await ledger.record_result(
                task_id=task_id,
                operation_key=OPERATION,
                lease_epoch=epoch,
                succeeded=True,
            )
        return captured.value.found_status

    assert _run(scenario) == "cancelled"


def test_reporting_a_result_twice_leaves_the_first_one() -> None:
    """A caller bug rather than a race, and the row survives it.

    A second report would overwrite a recorded outcome with a later guess --
    including overwriting ``needs_reconciliation``, which is exactly the state
    that must not be cleared by anything but a human.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        await ledger.mark_for_reconciliation(
            task_id=task_id,
            operation_key=OPERATION,
            lease_epoch=epoch,
            detail="the connection dropped mid-dispatch",
        )
        with pytest.raises(ToolExecutionNotWritableError) as captured:
            await ledger.record_result(
                task_id=task_id,
                operation_key=OPERATION,
                lease_epoch=epoch,
                succeeded=True,
            )
        stored = await ledger.get(task_id=task_id, operation_key=OPERATION)
        assert stored is not None
        return captured.value.found_status, stored.status

    assert _run(scenario) == ("needs_reconciliation", "needs_reconciliation")


# --------------------------------------------------------------------------
# What the database refuses on its own
# --------------------------------------------------------------------------


async def _insert_directly(
    engine: Any, task_id: str, epoch: int, row: dict[str, Any]
) -> None:
    """Write a row the adapter would never produce, bypassing it entirely."""

    values: dict[str, Any] = {
        "execution_id": "texec_direct",
        "task_id": task_id,
        "operation_key": "export:direct",
        "tool_name": "export_artifact",
        "canonical_request_hash": argument_digest(REQUEST),
        "lease_epoch": epoch,
        "agent_run_id": "run_1",
        "tool_call_id": "toolu_01",
        "policy_identity": "policy-1:ffffffffffffffff",
        **row,
    }
    settled_at = values.pop("settled_at")
    statement = text(
        "INSERT INTO tool_executions (execution_id, task_id, operation_key, "
        "tool_name, canonical_request_hash, status, lease_epoch, agent_run_id, "
        "tool_call_id, policy_identity, settled_at) VALUES (:execution_id, "
        ":task_id, :operation_key, :tool_name, :canonical_request_hash, :status, "
        ":lease_epoch, :agent_run_id, :tool_call_id, :policy_identity, "
        + ("now())" if settled_at == "now()" else "NULL)")
    )
    async with engine.begin() as connection:
        await connection.execute(statement, values)


@pytest.mark.parametrize(
    ("row", "constraint"),
    [
        # Settled without saying when, and unsettled while claiming to be.
        ({"status": "succeeded", "settled_at": None}, "settlement"),
        ({"status": "intended", "settled_at": "now()"}, "settlement"),
        # A status the domain does not define. It carries a settlement time on
        # purpose: without one the *settlement* constraint would reject it, and
        # the test would pass while the status vocabulary was unguarded. A
        # sabotage round found exactly that -- dropping the status CHECK in the
        # database changed nothing until this row was written this way.
        ({"status": "half_done", "settled_at": "now()"}, "status"),
        ({"status": "intended", "settled_at": None, "lease_epoch": 0}, "lease_epoch"),
    ],
)
def test_a_row_that_contradicts_itself_is_refused_by_the_database(
    row: dict[str, Any], constraint: str
) -> None:
    """Not by this adapter.

    An audit table whose invariants depend on one writer remembering them is not
    an audit table -- a later writer, a migration or a console session would all
    be outside it. Each case names the constraint it is aimed at, and asserts
    that *that* one rejected it: a row refused by the wrong constraint is a test
    that would survive its own subject being deleted.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> str:
        task_id, epoch = await _claimed(registry)
        with pytest.raises(IntegrityError) as captured:
            await _insert_directly(engine, task_id, epoch, row)
        return str(captured.value)

    assert f"tool_executions_{constraint}" in _run(scenario)


def test_the_database_accepts_exactly_the_statuses_the_ledger_defines() -> None:
    """Every status the port names is storable, and nothing else is.

    Both directions, because a vocabulary check that only refuses is one that
    would also pass if the column accepted nothing at all.
    """

    async def scenario(engine: Any, registry: Any, ledger: Any) -> tuple[Any, ...]:
        task_id, epoch = await _claimed(registry)
        accepted: list[str] = []
        for index, status in enumerate(sorted(get_args(ToolExecutionStatus))):
            # The settlement constraint is satisfied for each status, so the
            # only thing that can refuse a row here is the vocabulary itself.
            await _insert_directly(
                engine,
                task_id,
                epoch,
                {
                    "execution_id": f"texec_{index}",
                    "operation_key": f"export:{status}",
                    "status": status,
                    "settled_at": None if status == "intended" else "now()",
                },
            )
            accepted.append(status)
        return tuple(accepted), tuple(sorted(get_args(ToolExecutionStatus)))

    accepted, defined = _run(scenario)

    assert accepted == defined


def test_an_operation_cannot_outlive_the_task_it_belongs_to() -> None:
    """The foreign key is what makes the ledger readable beside the Task."""

    async def scenario(engine: Any, registry: Any, ledger: Any) -> None:
        task_id, epoch = await _claimed(registry)
        await ledger.record_intent(_intent(task_id, epoch=epoch))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM task_runs WHERE task_id = :t"), {"t": task_id}
                )

    _run(scenario)
