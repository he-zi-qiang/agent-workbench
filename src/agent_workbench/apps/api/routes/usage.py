"""What this tenant has spent, in one answer.

Three surfaces already report their own spend -- a Chat turn's footer, a Task's
right rail, a sub-agent's row in the agent panel -- and each of them reads the
run in front of it. None of them can answer "where did the money go this
month", because that question crosses every run this tenant has ever started
and none of those pages holds more than one.

**It reports and does not bill.** Every figure is summed out of the event log,
where the runtime wrote it at the time. Nothing is recomputed at read time, so
this endpoint cannot disagree with the run it came from -- and equally cannot
correct a run that was priced wrong. A deployment that fixes a stale price list
sees the new rates on new runs only. That is the same trade the domain's
pricing module already made, and stating it here keeps the console from
implying an authority it does not have.

**Money is not converted.** Costs leave as ``micro_usd`` integers, exactly as
stored. A rendering in yuan is a display decision that needs an exchange rate
this process does not have and must not invent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.ports.usage import USAGE_MODES, UsageSlice

router = APIRouter(prefix="/v1/usage", tags=["usage"])

#: The windows the console offers, and nothing else. An arbitrary `days=` would
#: look more general and would mostly be used to ask for a window nobody sized
#: the query for.
WINDOWS: dict[str, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


class TokenBreakdown(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    #: A subset of `input_tokens`, not an addition to it -- the provider's
    #: convention, carried through unchanged. Summing the two double counts
    #: every cached prompt, which is exactly the mistake a separate field here
    #: is meant to make visible rather than invite.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class UsageBucket(BaseModel):
    tokens: TokenBreakdown = TokenBreakdown()
    cost_micro_usd: int = 0
    runs: int = 0


class UsageResponse(BaseModel):
    window: Literal["7d", "30d", "all"]
    since: datetime | None
    until: datetime
    by_mode: dict[str, UsageBucket]
    by_model: dict[str, UsageBucket]
    #: Inside `by_mode["task"]`, not beside it. Sub-agent tokens are real spend
    #: on this bill but never counted against the parent run's budget, so a
    #: caller that adds these two numbers has produced a figure that means
    #: nothing.
    delegated: UsageBucket
    runs_in_flight: int = 0
    #: Profiles whose recorded cost was zero for every run in the window. The
    #: console says "no price list" rather than "free"; this field is what lets
    #: it, instead of inferring generosity from a zero.
    unpriced_profiles: tuple[str, ...] = ()


def _bucket(slice_: UsageSlice) -> UsageBucket:
    return UsageBucket(
        tokens=TokenBreakdown(
            input_tokens=slice_.tokens.input_tokens,
            output_tokens=slice_.tokens.output_tokens,
            cache_read_tokens=slice_.tokens.cache_read_tokens,
            cache_write_tokens=slice_.tokens.cache_write_tokens,
        ),
        cost_micro_usd=slice_.cost_micro_usd,
        runs=slice_.runs,
    )


@router.get("", response_model=UsageResponse)
async def usage(
    request: Request,
    window: Annotated[Literal["7d", "30d", "all"], Query()] = "30d",
) -> UsageResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)

    until = datetime.now(UTC)
    span = WINDOWS[window]
    since = None if span is None else until - span

    report = await dependencies.usage.report(
        tenant_id=principal.tenant_id,
        since=since,
        until=until,
    )

    # Every mode present, including the ones at zero. A page that shows only
    # the modes with spend cannot say "you have not used Code this month" --
    # the row simply is not there, which reads as a missing feature.
    by_mode = {
        mode: _bucket(report.by_mode.get(mode, UsageSlice())) for mode in USAGE_MODES
    }

    return UsageResponse(
        window=window,
        since=report.since,
        until=report.until,
        by_mode=by_mode,
        by_model={
            name: _bucket(slice_) for name, slice_ in sorted(report.by_model.items())
        },
        delegated=_bucket(report.delegated),
        runs_in_flight=report.runs_in_flight,
        unpriced_profiles=report.unpriced_profiles,
    )


__all__ = ["UsageBucket", "UsageResponse", "router"]
