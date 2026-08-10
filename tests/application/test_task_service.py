"""Submitting a Task, and asking about one that may not be yours.

Two things the Registry deliberately leaves to this layer are checked here: the
thread id is minted rather than accepted, and a read by id answers the same way
for "no such Task" and "not yours". The second is the one that matters -- a
different answer for the two *is* the disclosure.

No database. The Registry is a fake that records what it was handed, because
what is under test is what this layer decides, not what PostgreSQL stores.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.application.tasks import (
    DEFAULT_TIMELINE_LIMIT,
    MAX_TIMELINE_LIMIT,
    SubmittedSemantics,
    TaskService,
)
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.task_intent import TaskIntent
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission
from agent_workbench.workflows.general_graph import GRAPH_VERSION_V2
from agent_workbench.workflows.research_graph import GRAPH_VERSION_V1

SEMANTICS = SubmittedSemantics(
    run_semantics_snapshot={"model": {"provider": "deepseek"}},
    run_semantics_revision="1.2:v1.3:abc0123456789def",
    policy_revision="policy-1",
    policy_fingerprint="f" * 16,
    authorization_envelope=AuthorizationEnvelope(),
)

OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
OTHER_OWNER = PrincipalContext(principal_id="user_2", tenant_id="tenant_a")
OTHER_TENANT = PrincipalContext(principal_id="user_1", tenant_id="tenant_b")


class _FakeRegistry:
    """Records submissions and hands back whatever it was told to hold."""

    def __init__(self, existing: TaskRun | None = None) -> None:
        self.submissions: list[TaskSubmission] = []
        self._existing = existing

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        self.submissions.append(submission)
        return _task(**_stored(submission))

    async def get(self, task_id: str) -> TaskRun | None:
        return self._existing


class _IdempotentFakeRegistry(_FakeRegistry):
    """The Registry's public idempotency contract, without a database.

    PostgreSQL has its own race tests. This fake makes the application-level
    promise visible where TaskService chooses server-owned submission fields.
    """

    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[tuple[str, str, str], TaskRun] = {}

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        self.submissions.append(submission)
        key = (
            submission.tenant_id,
            submission.owner_id,
            submission.submission_dedup_key,
        )
        existing = self._by_key.get(key)
        if existing is not None:
            return existing
        opened = _task(**_stored(submission))
        self._by_key[key] = opened
        return opened


def _stored(submission: TaskSubmission) -> dict[str, Any]:
    """The row shape a submission becomes, as the real adapter writes it.

    The nested reservation flattens into three columns there, so the fake does
    the same -- otherwise it would accept a submission the database cannot
    store and hide exactly the mapping under test. Intent is dropped for the
    same fidelity: the adapter keeps it off the row and records it only on the
    TaskSubmitted event (ADR-036).
    """

    fields = submission.model_dump()
    reservation = fields.pop("index_reservation", None)
    fields.pop("intent", None)
    fields.update(
        {
            "resolved_qdrant_collection": (
                None if reservation is None else reservation["collection_name"]
            ),
            "resolved_qdrant_index_version": (
                None if reservation is None else reservation["index_version"]
            ),
            "resolved_qdrant_index_generation_id": (
                None if reservation is None else reservation["generation_id"]
            ),
        }
    )
    return fields


def _task(**overrides: Any) -> TaskRun:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    base: dict[str, Any] = {
        "task_id": "task_1",
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": GRAPH_VERSION_V1,
        "input_ref": "input_1",
        "input_fingerprint": "a" * 64,
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
        "status": "queued",
        "available_at": now,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return TaskRun.model_validate(base)


class _RecordingLog:
    """Records the read it was asked for, and returns nothing."""

    def __init__(self) -> None:
        self.limits: list[int] = []

    async def append(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the timeline never appends")

    async def read(
        self, stream_id: str, *, after_sequence: int | None = None, limit: int = 500
    ) -> tuple[Any, ...]:
        self.limits.append(limit)
        return ()


def _service(registry: Any, events: Any = None) -> TaskService:
    return TaskService(registry=registry, events=events, semantics=lambda: SEMANTICS)


# --------------------------------------------------------------------------
# Submitting


def test_the_caller_names_neither_the_thread_nor_the_graph_version() -> None:
    """Both are decisions, and neither belongs to a request.

    A caller-supplied thread would let a retry hand the Registry a *different*
    thread for the same key, which the unique constraint refuses -- turning an
    idempotent retry into an error. A caller-supplied version would let a
    client pin itself to a graph nobody deploys any more.
    """

    registry = _FakeRegistry()

    task = asyncio.run(
        _service(registry).submit(
            OWNER, input_ref="input_1", submission_dedup_key="dedup_1"
        )
    )

    submitted = registry.submissions[0]
    assert submitted.thread_id
    assert submitted.thread_id.startswith("thr_")
    assert submitted.graph_version == GRAPH_VERSION_V1
    assert task.thread_id == submitted.thread_id


def test_a_submission_that_chose_no_graph_is_the_deployment_default_exactly() -> None:
    """The anti-regression half of ADR-031 §2.3: absent means today's shape.

    Every existing client submits without the field, so the submission that
    reaches the Registry must be indistinguishable from one made before the
    choice existed -- same version, and nothing else about the row moved.
    """

    chosen = _FakeRegistry()
    unchosen = _FakeRegistry()

    asyncio.run(
        _service(unchosen).submit(
            OWNER, input_ref="input_1", submission_dedup_key="dedup_1"
        )
    )
    asyncio.run(
        _service(chosen).submit(
            OWNER, input_ref="input_1", submission_dedup_key="dedup_1", graph=None
        )
    )

    defaulted = unchosen.submissions[0]
    explicit_none = chosen.submissions[0]
    assert defaulted.graph_version == GRAPH_VERSION_V1
    # thread_id is minted per submission; identical everywhere else.
    assert defaulted.model_dump(exclude={"thread_id"}) == explicit_none.model_dump(
        exclude={"thread_id"}
    )


def test_a_submission_chooses_a_shape_and_the_service_maps_it_to_a_version() -> None:
    """The caller names ``research`` or ``general``, never a version string.

    Since ADR-036 the shape may have been proposed by triage, but what reaches
    this layer is always an explicit shape or nothing -- never "guess for me"
    -- so the mapping and the freeze work identically whoever chose. The
    *version* stays a deployment fact, which is why the mapping lives here.
    """

    registry = _FakeRegistry()
    service = _service(registry)

    asyncio.run(
        service.submit(
            OWNER, input_ref="input_1", submission_dedup_key="dedup_1", graph="general"
        )
    )
    asyncio.run(
        service.submit(
            OWNER, input_ref="input_2", submission_dedup_key="dedup_2", graph="research"
        )
    )

    assert registry.submissions[0].graph_version == GRAPH_VERSION_V2
    assert registry.submissions[1].graph_version == GRAPH_VERSION_V1


def test_intent_reaches_the_submission_and_stays_out_of_its_identity() -> None:
    """Provenance rides the submission; a retry without it is still a retry.

    The second half is the teeth: the idempotent fake keys only on the dedup
    key, and the service must return the original Task for a retry whose
    intent differs -- triage is not deterministic, and a retry that re-ran it
    must not become a conflict (ADR-036 §2.3).
    """

    registry = _IdempotentFakeRegistry()
    service = _service(registry)
    intent = TaskIntent(
        graph_decided_by="model", wants_report_decided_by="model", reason="像调研"
    )

    first = asyncio.run(
        service.submit(
            OWNER,
            input_ref="input_1",
            submission_dedup_key="dedup_1",
            graph="research",
            intent=intent,
        )
    )
    retried = asyncio.run(
        service.submit(
            OWNER,
            input_ref="input_1",
            submission_dedup_key="dedup_1",
            graph="research",
            intent=None,
        )
    )

    assert registry.submissions[0].intent == intent
    assert registry.submissions[1].intent is None
    assert retried.task_id == first.task_id


def test_an_explicit_choice_survives_a_changed_deployment_default() -> None:
    """The freeze (ADR-031 §2.3), at the layer that does the freezing.

    The version reaches the Registry resolved, in the submission row itself --
    so a Task's graph is a fact about the Task, and a deployment that later
    defaults to v2 changes only the submissions that did not choose. The
    Worker reads the row, never the configuration.
    """

    v2_defaulted = _FakeRegistry()
    service = TaskService(
        registry=v2_defaulted,  # type: ignore[arg-type]
        semantics=lambda: SEMANTICS,
        graph_version=GRAPH_VERSION_V2,
    )

    asyncio.run(
        service.submit(
            OWNER, input_ref="input_1", submission_dedup_key="dedup_1", graph="research"
        )
    )
    asyncio.run(
        service.submit(OWNER, input_ref="input_2", submission_dedup_key="dedup_2")
    )

    # The explicit choice ignored the default; the silent one took it.
    assert v2_defaulted.submissions[0].graph_version == GRAPH_VERSION_V1
    assert v2_defaulted.submissions[1].graph_version == GRAPH_VERSION_V2


def test_identity_comes_from_the_principal_and_not_from_the_request() -> None:
    registry = _FakeRegistry()

    asyncio.run(
        _service(registry).submit(
            OTHER_TENANT, input_ref="input_1", submission_dedup_key="dedup_1"
        )
    )

    submitted = registry.submissions[0]
    assert submitted.tenant_id == OTHER_TENANT.tenant_id
    assert submitted.owner_id == OTHER_TENANT.principal_id


def test_submission_captures_a_normalized_principal_scope_ceiling() -> None:
    registry = _FakeRegistry()
    principal = PrincipalContext(
        principal_id="user_1",
        tenant_id="tenant_a",
        scopes=("external:search", "knowledge:read", "external:search"),
    )

    asyncio.run(
        _service(registry).submit(
            principal, input_ref="input_1", submission_dedup_key="dedup_1"
        )
    )

    assert registry.submissions[0].submitted_principal_scopes == (
        "external:search",
        "knowledge:read",
    )


def test_a_retry_returns_the_original_task_despite_fresh_server_fields() -> None:
    """The service mints per-attempt values; the Registry owns idempotency.

    Minting one thread per call and letting the insert lose is what makes a
    repeated key idempotent rather than an error. A restarted deployment can
    choose different semantics too; neither fresh server decision may turn an
    ordinary caller retry into a conflict.
    """

    registry = _IdempotentFakeRegistry()
    changed_semantics = replace(
        SEMANTICS,
        run_semantics_revision="1.3:v1.4:0000000000000abc",
        policy_fingerprint="0" * 16,
    )
    decisions = iter((SEMANTICS, changed_semantics))
    service = TaskService(registry=registry, semantics=lambda: next(decisions))

    async def scenario() -> tuple[TaskRun, TaskRun]:
        first = await service.submit(OWNER, input_ref="i", submission_dedup_key="k")
        second = await service.submit(OWNER, input_ref="i", submission_dedup_key="k")
        return first, second

    first, second = asyncio.run(scenario())

    threads = {submission.thread_id for submission in registry.submissions}
    assert len(threads) == 2
    assert first == second
    assert second.thread_id == registry.submissions[0].thread_id


# --------------------------------------------------------------------------
# Reading


def test_an_owner_reads_their_own_task() -> None:
    task = asyncio.run(_service(_FakeRegistry(_task())).get(OWNER, "task_1"))

    assert task.task_id == "task_1"


@pytest.mark.parametrize(
    ("principal", "label"),
    [
        (OTHER_OWNER, "another owner in the same tenant"),
        (OTHER_TENANT, "the same owner id in another tenant"),
    ],
)
def test_a_task_that_is_not_yours_is_not_found(
    principal: PrincipalContext, label: str
) -> None:
    """Not "forbidden": that answer would confirm the id exists.

    Both halves of ownership are checked. A tenant match alone would expose one
    tenant's Tasks to everybody in it; an owner match alone would let an id
    collide across tenants into somebody else's Task.
    """

    with pytest.raises(NotFoundError):
        asyncio.run(_service(_FakeRegistry(_task())).get(principal, "task_1"))


def test_a_task_that_does_not_exist_fails_the_same_way() -> None:
    """The two answers have to be indistinguishable, so they are compared."""

    missing = _service(_FakeRegistry(None))
    forbidden = _service(_FakeRegistry(_task()))

    with pytest.raises(NotFoundError) as absent:
        asyncio.run(missing.get(OWNER, "task_1"))
    with pytest.raises(NotFoundError) as not_mine:
        asyncio.run(forbidden.get(OTHER_OWNER, "task_1"))

    assert type(absent.value) is type(not_mine.value)
    assert absent.value.code == not_mine.value.code
    assert str(absent.value) == str(not_mine.value)


# --------------------------------------------------------------------------
# What the timeline asks the log for


def test_an_oversized_limit_is_capped_before_it_reaches_the_log() -> None:
    """A read is a client-supplied request, and the cap is what bounds it.

    Asserted against what the log was *asked* for, because observing it
    through a real log would mean storing more events than the cap just to
    watch it apply.
    """

    log = _RecordingLog()
    service = _service(_FakeRegistry(_task()), log)

    asyncio.run(service.timeline(OWNER, "task_1", limit=MAX_TIMELINE_LIMIT * 10))

    assert log.limits == [MAX_TIMELINE_LIMIT]


def test_an_ordinary_limit_is_passed_through_unchanged() -> None:
    log = _RecordingLog()
    service = _service(_FakeRegistry(_task()), log)

    asyncio.run(service.timeline(OWNER, "task_1", limit=7))
    asyncio.run(service.timeline(OWNER, "task_1"))

    assert log.limits == [7, DEFAULT_TIMELINE_LIMIT]
