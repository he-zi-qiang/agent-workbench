"""Summing the log, once, in the database.

The whole report is four queries and no application-side accumulation. That is
deliberate: pulling every terminal event into the process to add it up would
make the cost of asking "what did I spend this month" grow with how much work
the tenant has ever done, and this endpoint is the one a person refreshes.

**Why the joins look asymmetric.** Task runs reach their tenant through
``task_runs.task_id``, which the event carries as a column. Chat and Code runs
reach theirs through ``chat_turns.run_id`` -> ``conversation_sessions``, and
their events have no ``task_id`` at all. Two shapes because the two products
are two shapes; forcing one join would mean inventing a column on ``events``,
and ``event_streams`` says in as many words why that column does not exist.

**Why the model profile comes from a second pass.** ``RunStarted`` names the
profile and the terminal event carries the account; they are different rows.
Joining them per run in SQL is possible and was tried -- it needs a lateral
join or a window over the whole events table, and both plans degraded badly
once a stream had a few thousand rows. Reading the profile map for the runs
that actually appear in the window is one indexed lookup and stays flat.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import (
    chat_turns,
    conversation_sessions,
    events,
    task_runs,
)
from agent_workbench.domain.runs import TokenUsage
from agent_workbench.ports.usage import (
    TERMINAL_EVENT_TYPES,
    UsageMode,
    UsageReport,
    UsageSlice,
)


def _int(value: object) -> int:
    """A JSONB number that survived a schema change, or zero.

    The payloads are written by a pydantic model, so these keys are present and
    numeric for every event this repository has ever emitted. `or zero` is for
    the row written by a version that predates a field -- replay must not fail
    on history, and a missing token count is genuinely zero of that token.
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _mapping(value: object) -> dict[str, object] | None:
    """A JSONB object, narrowed once.

    Cast rather than trust: a JSONB column is ``Any`` to the type checker, so
    every read out of one is unknown until something asserts a shape. Doing it
    here means the rest of the module reads plain typed dicts instead of
    repeating the same two lines at each key. Same move `task_registry` makes
    where it reads a ceiling out of a run-semantics snapshot.
    """

    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def _tokens_of(usage: object) -> TokenUsage:
    held = _mapping(usage)
    if held is None:
        return TokenUsage()
    raw = _mapping(held.get("tokens"))
    if raw is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=_int(raw.get("input_tokens")),
        output_tokens=_int(raw.get("output_tokens")),
        cache_read_tokens=_int(raw.get("cache_read_tokens")),
        cache_write_tokens=_int(raw.get("cache_write_tokens")),
    )


def _cost_of(usage: object) -> int:
    held = _mapping(usage)
    return 0 if held is None else _int(held.get("cost_micro_usd"))


class PostgresUsageReader:
    """`UsageReader` over the event log and the two tables that know tenancy."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def report(
        self,
        *,
        tenant_id: str,
        since: datetime | None,
        until: datetime,
    ) -> UsageReport:
        window = [events.c.recorded_at < until]
        if since is not None:
            window.append(events.c.recorded_at >= since)

        terminal = events.c.event_type.in_(TERMINAL_EVENT_TYPES)

        # Task: the event names its task, the task names its tenant.
        task_rows = (
            select(
                events.c.run_id,
                events.c.payload,
            )
            .select_from(
                events.join(task_runs, task_runs.c.task_id == events.c.task_id)
            )
            .where(and_(terminal, task_runs.c.tenant_id == tenant_id, *window))
        )

        # Chat and Code: the run belongs to a turn, the turn to a session, the
        # session to a tenant -- and the session's `mode` is the only place the
        # two products are told apart.
        session_rows = (
            select(
                events.c.run_id,
                events.c.payload,
                conversation_sessions.c.mode,
            )
            .select_from(
                events.join(chat_turns, chat_turns.c.run_id == events.c.run_id).join(
                    conversation_sessions,
                    conversation_sessions.c.session_id == chat_turns.c.session_id,
                )
            )
            .where(
                and_(
                    terminal,
                    conversation_sessions.c.tenant_id == tenant_id,
                    *window,
                )
            )
        )

        async with self._engine.connect() as connection:
            task_payloads = [
                (str(run_id), payload)
                for run_id, payload in (await connection.execute(task_rows)).all()
            ]
            session_payloads = [
                (str(run_id), payload, str(mode))
                for run_id, payload, mode in (
                    await connection.execute(session_rows)
                ).all()
            ]
            seen = [run_id for run_id, _ in task_payloads]
            seen.extend(run_id for run_id, _, _ in session_payloads)
            profiles = await _profiles_for(connection, seen)
            delegated_ids = await _delegated_run_ids(connection, seen)
            in_flight = await _in_flight_count(
                connection, tenant_id=tenant_id, until=until
            )

        return build_report(
            task_payloads=task_payloads,
            session_payloads=session_payloads,
            profiles=profiles,
            delegated_ids=delegated_ids,
            runs_in_flight=in_flight,
            since=since,
            until=until,
        )


def build_report(
    *,
    task_payloads: list[tuple[str, object]],
    session_payloads: list[tuple[str, object, str]],
    profiles: dict[str, str],
    delegated_ids: set[str],
    runs_in_flight: int,
    since: datetime | None,
    until: datetime,
) -> UsageReport:
    """Fold the rows into the report, with no database in sight.

    Separated from the queries above on purpose. The arithmetic here is where
    the mistakes live -- double counting a delegated run, bucketing an unknown
    session mode into `chat`, calling an unpriced total "free" -- and CI's
    quality job runs offline, so anything reachable only through a real
    PostgreSQL connection is not covered by it. This function is reachable with
    a list of tuples.
    """

    by_mode: dict[UsageMode, UsageSlice] = {}
    by_run: dict[str, UsageSlice] = {}

    for run_id, payload in task_payloads:
        slice_ = _slice_of(payload)
        by_mode["task"] = by_mode.get("task", UsageSlice()).merged(slice_)
        by_run[run_id] = slice_

    for run_id, payload, mode in session_payloads:
        # A session mode this build does not know about is dropped rather than
        # bucketed into `chat`: a wrong column is worse than a missing one, and
        # the totals stay checkable against the log.
        if mode not in ("chat", "code"):
            continue
        resolved: UsageMode = "chat" if mode == "chat" else "code"
        slice_ = _slice_of(payload)
        by_mode[resolved] = by_mode.get(resolved, UsageSlice()).merged(slice_)
        by_run[run_id] = slice_

    by_model: dict[str, UsageSlice] = {}
    for run_id, slice_ in by_run.items():
        profile = profiles.get(run_id)
        if profile is None:
            continue
        by_model[profile] = by_model.get(profile, UsageSlice()).merged(slice_)

    delegated = UsageSlice()
    for run_id in sorted(delegated_ids):
        entry = by_run.get(run_id)
        if entry is not None:
            delegated = delegated.merged(entry)

    # Zero recorded cost across every run on a profile. Almost always "no
    # prices configured"; a genuinely free model looks identical, and both
    # want the same footnote rather than a total that reads as a bargain.
    unpriced = tuple(
        sorted(name for name, slice_ in by_model.items() if slice_.cost_micro_usd == 0)
    )

    return UsageReport(
        by_mode=by_mode,
        by_model=by_model,
        delegated=delegated,
        runs_in_flight=runs_in_flight,
        unpriced_profiles=unpriced,
        since=since,
        until=until,
    )


def _slice_of(payload: object) -> UsageSlice:
    held = _mapping(payload)
    usage: object = None if held is None else held.get("usage")
    return UsageSlice(tokens=_tokens_of(usage), cost_micro_usd=_cost_of(usage), runs=1)


async def _profiles_for(
    connection: AsyncConnection, run_ids: list[str]
) -> dict[str, str]:
    """Which model profile each of these runs declared when it started."""

    if not run_ids:
        return {}
    rows = await connection.execute(
        select(events.c.run_id, events.c.payload).where(
            and_(
                events.c.event_type == "RunStarted",
                events.c.run_id.in_(run_ids),
            )
        )
    )
    found: dict[str, str] = {}
    for run_id, payload in rows.all():
        held = _mapping(payload)
        if held is None:
            continue
        profile = held.get("model_profile")
        if isinstance(profile, str) and profile:
            found[str(run_id)] = profile
    return found


async def _delegated_run_ids(
    connection: AsyncConnection, run_ids: list[str]
) -> set[str]:
    """Which of these runs were started by another run rather than by a person.

    Read from ``AgentDelegated`` because that event is the only thing that
    knows: a child's own events carry its run id and nothing about who sent it.

    The child id lives inside the JSONB payload, and the filtering happens in
    Python rather than in a `payload ->> 'child_agent_run_id' IN (...)`. Not for
    clarity -- for cost: that predicate cannot use any index this schema
    declares, so it degrades into a full scan of `events` exactly as the log
    grows. Delegations are rare (one row per sub-agent ever started), so
    fetching them all and intersecting is bounded by how much delegating this
    deployment has done, not by how many events it has written.
    """

    if not run_ids:
        return set()
    wanted = set(run_ids)
    rows = await connection.execute(
        select(events.c.payload).where(events.c.event_type == "AgentDelegated")
    )
    children: set[str] = set()
    for (payload,) in rows.all():
        held = _mapping(payload)
        if held is None:
            continue
        child = held.get("child_agent_run_id")
        if isinstance(child, str) and child in wanted:
            children.add(child)
    return children


async def _in_flight_count(
    connection: AsyncConnection, *, tenant_id: str, until: datetime
) -> int:
    """Runs that started before the window closed and never finished.

    Counted from the same two joins, so a run this tenant cannot see is not in
    the caveat either.
    """

    finished = select(events.c.run_id).where(
        events.c.event_type.in_(TERMINAL_EVENT_TYPES)
    )
    reachable = (
        select(events.c.run_id)
        .select_from(events.join(task_runs, task_runs.c.task_id == events.c.task_id))
        .where(task_runs.c.tenant_id == tenant_id)
        .union(
            select(events.c.run_id)
            .select_from(
                events.join(chat_turns, chat_turns.c.run_id == events.c.run_id).join(
                    conversation_sessions,
                    conversation_sessions.c.session_id == chat_turns.c.session_id,
                )
            )
            .where(conversation_sessions.c.tenant_id == tenant_id)
        )
    )
    counted = (
        select(events.c.run_id)
        .where(
            and_(
                events.c.event_type == "RunStarted",
                events.c.recorded_at < until,
                events.c.run_id.in_(reachable),
                events.c.run_id.not_in(finished),
            )
        )
        .distinct()
        .subquery()
    )
    rows = await connection.execute(select(func.count()).select_from(counted))
    return int(rows.scalar_one())


__all__ = ["PostgresUsageReader", "build_report"]
