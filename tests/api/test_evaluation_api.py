"""Reading evaluation reports over HTTP, and refusing to start one honestly.

The launcher is a fake for the same reason the code API's executor is: what is
under test here is which requests are accepted and what they answer, and a real
runner would put that behind thirty minutes of BGE-M3.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent_workbench.adapters.evaluation import COMMANDS, SubprocessEvaluationLauncher
from agent_workbench.application.evaluation import EvaluationService
from agent_workbench.apps.api.main import ERROR_STATUS
from agent_workbench.apps.api.routes import evaluation as evaluation_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.evaluation_runs import (
    EvaluationBusyError,
    EvaluationDisabledError,
    EvaluationRunState,
    EvaluationSuite,
)

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


class _StubPrincipals:
    def resolve(self, request: object) -> object:
        return PRINCIPAL


class _FakeLauncher:
    """Records what it was asked to start; never spawns anything."""

    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.started: list[EvaluationSuite] = []
        self.cancelled = 0
        self._state: EvaluationRunState | None = None

    async def start(self, suite: EvaluationSuite) -> EvaluationRunState:
        if self.busy:
            raise EvaluationBusyError("already running")
        self.started.append(suite)
        self._state = EvaluationRunState(
            suite=suite,
            status="running",
            started_at=datetime.now(UTC),
            finished_at=None,
            exit_code=None,
            recent_output=("indexing corpus",),
        )
        return self._state

    def state(self) -> EvaluationRunState | None:
        return self._state

    async def cancel(self) -> None:
        self.cancelled += 1


def _app(service: EvaluationService) -> FastAPI:
    app = FastAPI()
    app.include_router(evaluation_route.router)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(principals=_StubPrincipals(), evaluation=service),
    )
    for failure in (EvaluationBusyError, EvaluationDisabledError):
        app.add_exception_handler(
            failure,
            _refuse(ERROR_STATUS[failure]),  # pyright: ignore[reportArgumentType]
        )
    return app


def _refuse(status: int) -> Any:
    def handler(_request: Any, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return handler


def _run(service: EvaluationService, scenario: Any) -> Any:
    async def execute() -> Any:
        transport = httpx.ASGITransport(app=_app(service))  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def _service(
    root: Path, *, runs_enabled: bool = True, launcher: _FakeLauncher | None = None
) -> EvaluationService:
    return EvaluationService(
        launcher=launcher or _FakeLauncher(),  # pyright: ignore[reportArgumentType]
        reports_root=root,
        runs_enabled=runs_enabled,
    )


def _write_report(
    root: Path, relative: str, name: str, payload: dict[str, Any]
) -> None:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_the_reports_endpoint_reads_the_files_on_disk(tmp_path: Path) -> None:
    """And passes the metric names through exactly as the runner wrote them."""

    _write_report(
        tmp_path,
        "rag/reports",
        "hybrid-reference",
        {"question_count": 52, "scores": {"recall_at_3": 0.96, "mrr": 0.91}},
    )

    async def scenario(client: httpx.AsyncClient) -> list[dict[str, Any]]:
        answered = await client.get(
            f"{evaluation_route.EVALUATION_PREFIX}/reports", headers=HEADERS
        )
        return list(answered.json()["reports"])

    reports = _run(_service(tmp_path), scenario)

    assert len(reports) == 1
    assert reports[0]["suite"] == "rag"
    assert reports[0]["name"] == "hybrid-reference"
    # Verbatim. ADR-039: a metric name is a promise about how a number was
    # computed, so renaming or normalising one here would be this layer making
    # a promise it did not measure.
    assert reports[0]["payload"]["scores"] == {"recall_at_3": 0.96, "mrr": 0.91}
    # And the rest of the object, which the console needs to say which
    # question set a number answered.
    assert reports[0]["payload"]["question_count"] == 52


def test_nothing_has_been_run_is_not_an_error(tmp_path: Path) -> None:
    """The control for the row above: an empty tree is an empty list, not a 500."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[Any]]:
        answered = await client.get(
            f"{evaluation_route.EVALUATION_PREFIX}/reports", headers=HEADERS
        )
        return answered.status_code, list(answered.json()["reports"])

    assert _run(_service(tmp_path), scenario) == (200, [])


def test_a_file_that_is_not_a_report_does_not_take_the_page_down(
    tmp_path: Path,
) -> None:
    """This directory is written by scripts and read by somebody's editor."""

    (tmp_path / "rag/reports").mkdir(parents=True)
    (tmp_path / "rag/reports/notes.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "rag/reports/detail-outcomes.json").write_text(
        json.dumps({"outcomes": []}), encoding="utf-8"
    )
    _write_report(tmp_path, "rag/reports", "hybrid", {"scores": {"mrr": 0.9}})

    async def scenario(client: httpx.AsyncClient) -> list[str]:
        answered = await client.get(
            f"{evaluation_route.EVALUATION_PREFIX}/reports", headers=HEADERS
        )
        return [report["name"] for report in answered.json()["reports"]]

    # One stray file must not hide the reports beside it. The per-question dump
    # is excluded by the same rule rather than by its name: it carries no
    # `scores` key, so nothing has to know what `-outcomes` means.
    assert _run(_service(tmp_path), scenario) == ["hybrid"]


def test_a_deployment_that_cannot_run_says_so_instead_of_pretending(
    tmp_path: Path,
) -> None:
    _write_report(tmp_path, "rag/reports", "hybrid", {"scores": {"mrr": 0.9}})

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str, int, bool]:
        refused = await client.post(
            f"{evaluation_route.EVALUATION_PREFIX}/runs",
            headers=HEADERS,
            json={"suite": "rag"},
        )
        readable = await client.get(
            f"{evaluation_route.EVALUATION_PREFIX}/reports", headers=HEADERS
        )
        return (
            refused.status_code,
            refused.json()["detail"],
            readable.status_code,
            bool(readable.json()["runs_enabled"]),
        )

    status, detail, reports_status, enabled = _run(
        _service(tmp_path, runs_enabled=False), scenario
    )

    assert status == 503
    # The command, not just a refusal. A reader told only "not enabled" has to
    # go and find the invocation in a docstring.
    assert "scripts/run_rag_eval.py" in detail
    # And reading is untouched: the numbers that were committed are the only
    # evidence a deployment like this has.
    assert (reports_status, enabled) == (200, False)


def test_a_second_run_is_refused_while_one_is_live(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        answered = await client.post(
            f"{evaluation_route.EVALUATION_PREFIX}/runs",
            headers=HEADERS,
            json={"suite": "rag"},
        )
        return answered.status_code

    assert _run(_service(tmp_path, launcher=_FakeLauncher(busy=True)), scenario) == 409


def test_a_started_run_answers_immediately_and_is_readable_after(
    tmp_path: Path,
) -> None:
    launcher = _FakeLauncher()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str, str]:
        started = await client.post(
            f"{evaluation_route.EVALUATION_PREFIX}/runs",
            headers=HEADERS,
            json={"suite": "triage"},
        )
        current = await client.get(
            f"{evaluation_route.EVALUATION_PREFIX}/runs/current", headers=HEADERS
        )
        return (
            started.status_code,
            started.json()["status"],
            current.json()["run"]["suite"],
        )

    # 202: accepted, not performed. A full ablation is tens of minutes, and a
    # 200 would mean the request had waited for it.
    assert _run(_service(tmp_path, launcher=launcher), scenario) == (
        202,
        "running",
        "triage",
    )
    assert launcher.started == ["triage"]


def test_the_suite_name_never_reaches_a_command_line(tmp_path: Path) -> None:
    launcher = _FakeLauncher()

    async def scenario(client: httpx.AsyncClient) -> int:
        answered = await client.post(
            f"{evaluation_route.EVALUATION_PREFIX}/runs",
            headers=HEADERS,
            json={"suite": "rag; rm -rf /"},
        )
        return answered.status_code

    # Refused by the schema, before any code sees it.
    assert _run(_service(tmp_path, launcher=launcher), scenario) == 422
    assert launcher.started == []


def test_the_launcher_runs_a_fixed_command_per_suite() -> None:
    """The other half of the assertion above, at the layer that builds argv.

    A 422 proves the name did not get through *today*. This proves there is no
    string formatting for it to get through even if the schema were widened.
    """

    assert COMMANDS["rag"] == ("scripts/run_rag_eval.py",)
    assert COMMANDS["chat"] == ("scripts/run_chat_eval.py",)
    assert COMMANDS["triage"] == ("scripts/run_triage_eval.py",)
    assert set(COMMANDS) == {"rag", "chat", "triage"}


def test_the_committed_reports_still_parse() -> None:
    """The build gate that used to live in `reports.ts`, moved here.

    Deleting one of these used to break `tsc`, because the console imported
    them at build time. It reads them over HTTP now, so that check had to go
    somewhere -- and this is that somewhere, not a check that was dropped.
    """

    root = Path(__file__).resolve().parents[2] / "evals"
    service = EvaluationService(
        launcher=_FakeLauncher(),  # pyright: ignore[reportArgumentType]
        reports_root=root,
        runs_enabled=False,
    )
    names = {report.name for report in service.reports() if report.suite == "rag"}

    assert {
        "dense-llama_index",
        "dense-reference",
        "hybrid-llama_index",
        "hybrid-reference",
    } <= names


@pytest.mark.parametrize("suite", ["rag", "chat", "triage"])
def test_every_suite_has_a_runner_script_on_disk(suite: str) -> None:
    """A command naming a script that is not there would 503 at run time."""

    root = Path(__file__).resolve().parents[2]
    (script,) = COMMANDS[suite]  # pyright: ignore[reportArgumentType]
    assert (root / script).is_file()


def test_cancelling_when_nothing_runs_is_not_an_error(tmp_path: Path) -> None:
    launcher = _FakeLauncher()

    async def scenario(client: httpx.AsyncClient) -> int:
        answered = await client.post(
            f"{evaluation_route.EVALUATION_PREFIX}/runs/current/cancel", headers=HEADERS
        )
        return answered.status_code

    assert _run(_service(tmp_path, launcher=launcher), scenario) == 204
    assert launcher.cancelled == 1


def test_a_real_launcher_reports_a_failing_command_rather_than_raising(
    tmp_path: Path,
) -> None:
    """The honest failure mode when the embedding extra is missing.

    Uses the real launcher against a script that is not there, which is what a
    deployment without the runners looks like: the child exits non-zero within
    seconds and its own output is what the console shows.
    """

    launcher = SubprocessEvaluationLauncher(project_root=tmp_path, timeout_seconds=30)

    async def scenario() -> tuple[str, int | None]:
        await launcher.start("rag")
        for _ in range(100):
            state = launcher.state()
            assert state is not None
            if state.status != "running":
                return state.status, state.exit_code
            await asyncio.sleep(0.05)
        raise AssertionError("the run never finished")

    status, exit_code = asyncio.run(scenario())

    assert status == "failed"
    assert exit_code not in (0, None)
