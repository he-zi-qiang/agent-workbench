"""What a Task has spent on agent invocations, and that it does not forget.

ADR-040, second of three steps. Nothing here refuses anything: the counter is
written and readable, and no ceiling is applied yet. So these tests pin two
properties and no more -- the count follows the invocations, and it survives
the two events that a number living in a process would not survive.

The retry test is the one that matters. ``attempt_count`` already existed and
already survived a retry, so a counter that merely went up would look correct
while measuring the wrong thing; that test therefore asserts the two move
*differently*. Every "it refuses" assertion is paired with one showing the same
harness can observe the opposite outcome.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from sqlalchemy import select, text

from agent_workbench.adapters.persistence import (
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import task_runs
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.task_registry import (
    AgentInvocationBudgetExhaustedError,
    AgentInvocationCeilingMissingError,
    ExecutionLease,
    StaleExecutionError,
    TaskSubmission,
)
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.task_handlers import BudgetedAgentExecutor

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[Any, PostgresTaskRegistry], Awaitable[Any]]) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE task_runs, events, event_streams, "
                        "qdrant_index_generations CASCADE"
                    )
                )
            return await scenario(engine, PostgresTaskRegistry(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _submission(ceiling: int | None = 12) -> TaskSubmission:
    """A Task whose own snapshot says what it may spend.

    ``ceiling`` is a parameter because the ceiling is read from the Task rather
    than from the process: a test that set it in configuration would be
    describing a deployment, and what the ceiling actually binds to is this
    row. ``None`` produces a snapshot with no ceiling at all, which is its own
    refusal and has its own test.
    """

    semantics: dict[str, Any] = {"model": {"provider": "deepseek"}}
    if ceiling is not None:
        semantics["multi_agent"] = {"max_agent_invocation_attempts_per_task": ceiling}
    return TaskSubmission.model_validate(
        {
            "tenant_id": "tenant_a",
            "owner_id": "user_1",
            "thread_id": "thr_budget",
            "graph_version": "v1",
            "input_ref": "input_budget",
            "input_fingerprint": hashlib.sha256(b"input_budget").hexdigest(),
            "submission_dedup_key": "dedup_budget",
            "run_semantics_snapshot": semantics,
            "run_semantics_revision": "1.2:v1.3:abc0123456789def",
            "submitted_policy_revision": "policy-1",
            "submitted_policy_fingerprint": "f" * 16,
            "submitted_authorization_envelope": {},
            "submitted_principal_scopes": [],
        }
    )


async def _counts(engine: Any, task_id: str) -> tuple[int, int]:
    """Read both counters straight from the row.

    Not through ``registry.get``: this is asserting what the database holds, and
    a reader that goes through the layer under test would still agree with it if
    that layer were the thing that was wrong.
    """

    async with engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    select(
                        task_runs.c.agent_invocation_count,
                        task_runs.c.attempt_count,
                    ).where(task_runs.c.task_id == task_id)
                )
            )
            .mappings()
            .one()
        )
    return int(row["agent_invocation_count"]), int(row["attempt_count"])


class _Executor:
    """Stands in for a real agent run, and records that it happened."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, request: Any, emit: Any, cancellation: Any) -> Any:
        self.runs += 1
        return object()


def test_a_task_is_charged_once_for_each_agent_invocation() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None

        totals = [
            await registry.reserve_agent_invocation(claim.lease) for _ in range(3)
        ]

        assert totals == [1, 2, 3]
        spent, _ = await _counts(engine, task.task_id)
        assert spent == 3

    _run(scenario)


def test_a_task_that_invoked_nothing_is_charged_nothing() -> None:
    """The control for the counter.

    Without it, "the count is three after three invocations" would also hold on
    an implementation that charged on claim, or on one that returned a number
    unrelated to the column.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None

        spent, attempts = await _counts(engine, task.task_id)

        assert spent == 0
        # The claim itself did happen -- so a zero above is "nothing was
        # invoked", not "nothing ran at all".
        assert attempts == 1

    _run(scenario)


def test_what_a_task_already_spent_survives_a_retry() -> None:
    """The property a number passed into a process cannot have.

    ``attempt_count`` is asserted alongside precisely because it already
    survived retries: if both counters simply went up together, this test would
    pass on an implementation that had aliased the new counter to the old one.
    Here the Task is retried once and invoked twice, and the two numbers must
    end up different.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        first = await registry.claim_next("worker_1", lease_seconds=60)
        assert first is not None
        await registry.reserve_agent_invocation(first.lease)
        await registry.release_for_retry(first.lease, delay_seconds=0)

        second = await registry.claim_next("worker_2", lease_seconds=60)
        assert second is not None
        after_retry = await registry.reserve_agent_invocation(second.lease)

        # Continued from one, not restarted at one.
        assert after_retry == 2
        spent, attempts = await _counts(engine, task.task_id)
        assert spent == 2
        assert attempts == 2
        # Same numbers here by coincidence of the scenario, so the claim that
        # they are different counters is made where it can be seen: one more
        # invocation under the same claim moves only one of them.
        await registry.reserve_agent_invocation(second.lease)
        spent, attempts = await _counts(engine, task.task_id)
        assert (spent, attempts) == (3, 2)

    _run(scenario)


def test_a_worker_that_lost_its_claim_cannot_charge_the_task() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        stale = ExecutionLease(
            task_id=claim.lease.task_id,
            worker_id=claim.lease.worker_id,
            epoch=claim.lease.epoch + 1,
        )

        with pytest.raises(StaleExecutionError):
            await registry.reserve_agent_invocation(stale)

        # Refusing must not have spent anything either.
        spent, _ = await _counts(engine, task.task_id)
        assert spent == 0
        # And the live lease still works, so the refusal above was about the
        # epoch rather than about the method being broken.
        assert await registry.reserve_agent_invocation(claim.lease) == 1

    _run(scenario)


def test_the_executor_charges_the_task_it_is_running_for() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        inner = _Executor()
        scope = TaskExecutionScope()
        executor = BudgetedAgentExecutor(
            cast("AgentExecutor", inner), registry=registry, scope=scope
        )

        with scope.executing(claim.lease):
            await executor.run(
                cast("Any", object()), cast("Any", None), cast("Any", None)
            )
            await executor.run(
                cast("Any", object()), cast("Any", None), cast("Any", None)
            )

        assert inner.runs == 2
        spent, _ = await _counts(engine, task.task_id)
        assert spent == 2

    _run(scenario)


def test_an_executor_with_no_claim_in_scope_runs_and_charges_nobody() -> None:
    """The control for the executor.

    A decorator that raised or skipped the run whenever it could not bill would
    also satisfy the test above. What must happen instead is that the run
    proceeds and nothing is charged -- there is no authority to bill, and
    inventing one would be worse than not billing.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        inner = _Executor()
        executor = BudgetedAgentExecutor(
            cast("AgentExecutor", inner),
            registry=registry,
            scope=TaskExecutionScope(),
        )

        await executor.run(cast("Any", object()), cast("Any", None), cast("Any", None))

        assert inner.runs == 1
        spent, _ = await _counts(engine, task.task_id)
        assert spent == 0

    _run(scenario)


# --------------------------------------------------------------------------
# ADR-040 third step: the three refusals, and that they stay distinguishable


def test_a_task_that_spent_its_whole_allowance_is_refused() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission(ceiling=2))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None

        assert await registry.reserve_agent_invocation(claim.lease) == 1
        assert await registry.reserve_agent_invocation(claim.lease) == 2
        with pytest.raises(AgentInvocationBudgetExhaustedError) as refusal:
            await registry.reserve_agent_invocation(claim.lease)

        assert refusal.value.spent == 2
        assert refusal.value.ceiling == 2
        # The refusal did not also spend one. A gate that charged for being
        # refused would push the counter past its own ceiling forever.
        spent, _ = await _counts(engine, task.task_id)
        assert spent == 2

    _run(scenario)


def test_the_ceiling_comes_from_the_task_not_from_this_process() -> None:
    """The control for the ceiling.

    Same code, same process, two Tasks submitted with different allowances.
    If the ceiling were read from configuration, both would refuse at the same
    number and this test would fail while the one above still passed.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission(ceiling=1))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.reserve_agent_invocation(claim.lease)
        with pytest.raises(AgentInvocationBudgetExhaustedError) as tight:
            await registry.reserve_agent_invocation(claim.lease)

        assert tight.value.ceiling == 1
        # A second, more generous Task in the same process keeps going past
        # the point the first one stopped.
        await registry.mark_dead_lettered(claim.lease, reason="spent it all")
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE task_runs CASCADE"))
        await registry.submit(_submission(ceiling=5))
        generous = await registry.claim_next("worker_1", lease_seconds=60)
        assert generous is not None
        for expected in (1, 2, 3):
            assert await registry.reserve_agent_invocation(generous.lease) == expected

    _run(scenario)


def test_a_task_whose_snapshot_names_no_ceiling_is_a_deployment_defect() -> None:
    """Not the Task's fault, so not the Task's death.

    Dead-lettering this would turn one bad submission path into a batch of
    Tasks nobody can revive, so it raises a different type -- and the Worker
    turns that one into ``failed``.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        task = await registry.submit(_submission(ceiling=None))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None

        with pytest.raises(AgentInvocationCeilingMissingError):
            await registry.reserve_agent_invocation(claim.lease)

        spent, _ = await _counts(engine, task.task_id)
        assert spent == 0

    _run(scenario)


def test_losing_the_claim_is_reported_before_running_out_of_budget() -> None:
    """Order matters, and this is where it is asserted.

    A Worker that has both lost its lease *and* filled the counter must hear
    about the lease. The other way round it would dead-letter a Task that
    belongs to somebody else -- writing a terminal status under a claim it no
    longer holds, which is exactly what the fence exists to stop.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission(ceiling=1))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.reserve_agent_invocation(claim.lease)
        stale = ExecutionLease(
            task_id=claim.lease.task_id,
            worker_id=claim.lease.worker_id,
            epoch=claim.lease.epoch + 1,
        )

        # Budget is full *and* the epoch is wrong. Stale wins.
        with pytest.raises(StaleExecutionError):
            await registry.reserve_agent_invocation(stale)
        # And with a live lease the same call reports the budget instead, so
        # the assertion above is about precedence rather than about the epoch
        # being the only thing this method ever notices.
        with pytest.raises(AgentInvocationBudgetExhaustedError):
            await registry.reserve_agent_invocation(claim.lease)

    _run(scenario)


def test_the_two_writers_of_dead_letter_do_not_sound_alike() -> None:
    """ADR-040 §2.8's actual requirement, asserted rather than assumed.

    The reaper writes ``lease expired after N attempts``. If this writer's
    sentence were confusable with that one, an operator reading a dead-lettered
    Task could not tell a poison Task from an outage -- and a gate nobody can
    attribute is a gate that might as well be destroying Tasks quietly.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission(ceiling=1))
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None

        retired = await registry.mark_dead_lettered(
            claim.lease,
            reason="agent invocation budget exhausted: spent 1 of 1 allowed",
        )

        assert retired.status == "dead_letter"
        assert retired.status_detail is not None
        assert "budget" in retired.status_detail
        assert "lease expired" not in retired.status_detail
        # The event says which writer it was, and the reaper's value is not it.
        async with engine.begin() as connection:
            payloads = (
                (
                    await connection.execute(
                        text(
                            "SELECT payload FROM events "
                            "WHERE payload->>'kind' = 'TaskDeadLettered'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [p["reason_code"] for p in payloads] == ["invocation_budget_exhausted"]

    _run(scenario)
