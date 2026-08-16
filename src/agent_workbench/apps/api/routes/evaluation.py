"""Reading evaluation reports, and starting a run when the deployment allows it.

Mounted unconditionally, unlike the code router: reading always works, and only
`POST` is gated. A deployment that cannot launch a run can still show the
numbers that were committed, and hiding those as well would hide the only
evidence it has.

**One deviation, stated rather than left silent**: these responses are the same
for every principal in this deployment. Every other route here scopes to a
tenant and an owner because it serves somebody's data; an evaluation report is a
file in this repository's git tree, so scoping it would be scoping the README.
A principal is still resolved, because an unauthenticated request is still a
request this API does not answer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict

from agent_workbench.application.evaluation import HOW_TO_RUN, EvaluationService
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.ports.evaluation_runs import (
    EvaluationStatus,
    EvaluationSuite,
)

EVALUATION_PREFIX = "/v1/evaluation"

router = APIRouter(prefix=EVALUATION_PREFIX, tags=["evaluation"])


def _evaluation(request: Request) -> EvaluationService:
    service = dependencies_of(request).evaluation
    if service is None:  # pragma: no cover - assembled in every profile
        raise NotFoundError("this process serves no evaluation reports")
    return service


class ReportView(BaseModel):
    """One report file, passed through whole.

    ``payload`` is an open mapping and nothing in it is renamed, reordered or
    dropped. ADR-039 is the reason: a metric name is a promise about how a
    number was computed, and the same holds for the digest that says which
    question set it answered. This serves what the runner wrote, or nothing.
    """

    suite: EvaluationSuite
    name: str
    payload: dict[str, Any]


class ReportsResponse(BaseModel):
    reports: tuple[ReportView, ...]
    #: Whether this deployment will start a run. The console needs it to decide
    #: between a button and a command, and getting it from the same response as
    #: the reports means the page never renders a button it then has to retract.
    runs_enabled: bool
    #: What to type instead, per suite. Present even when runs are enabled: a
    #: reader who wants to run one in a terminal should not have to go and find
    #: the invocation in a docstring.
    how_to_run: dict[str, str]


class RunView(BaseModel):
    suite: EvaluationSuite
    status: EvaluationStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    exit_code: int | None
    recent_output: tuple[str, ...]


class CurrentRunResponse(BaseModel):
    """The run this process is doing, or has done since it started.

    ``null`` does not mean nothing has ever been run -- reports from earlier
    processes are still on disk. It means only that this process has not started
    one, which is exactly what a run that dies with its process can promise.
    """

    run: RunView | None


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A `Literal`, so an unknown suite is a 422 from the schema and never
    #: reaches the server's argv table.
    suite: EvaluationSuite


@router.get("/reports")
async def reports(request: Request) -> ReportsResponse:
    dependencies_of(request).principals.resolve(request)
    service = _evaluation(request)
    return ReportsResponse(
        reports=tuple(
            ReportView(suite=report.suite, name=report.name, payload=report.payload)
            for report in service.reports()
        ),
        runs_enabled=service.runs_enabled,
        # Widened to `str` keys deliberately: this crosses to JSON, where
        # every key is a string anyway, and a `Literal`-keyed dict would
        # make the response model depend on the suite enum in a way that
        # buys nothing a reader of the JSON can see.
        how_to_run={suite: command for suite, command in HOW_TO_RUN.items()},
    )


@router.get("/runs/current")
async def current_run(request: Request) -> CurrentRunResponse:
    dependencies_of(request).principals.resolve(request)
    state = _evaluation(request).current()
    return CurrentRunResponse(run=None if state is None else _view(state))


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(body: StartRunRequest, request: Request) -> RunView:
    """Begin a run and answer immediately.

    202, not 200: the run has been accepted, not performed. A full ablation is
    tens of minutes, and a response that waited for it would be a request held
    open past every proxy timeout between here and the browser.
    """

    dependencies_of(request).principals.resolve(request)
    return _view(await _evaluation(request).start(body.suite))


@router.post("/runs/current/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_run(request: Request) -> None:
    dependencies_of(request).principals.resolve(request)
    await _evaluation(request).cancel()


def _view(state: Any) -> RunView:
    return RunView(
        suite=state.suite,
        status=state.status,
        started_at=state.started_at,
        finished_at=state.finished_at,
        exit_code=state.exit_code,
        recent_output=state.recent_output,
    )


__all__ = ["EVALUATION_PREFIX", "router"]
