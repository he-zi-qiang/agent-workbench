"""What a console is told about delegation before it submits anything.

No database and no Worker: this endpoint answers out of the projection the
process loaded at start, so the whole harness is the router plus a stub
identity adapter. That is also why these run in CI while the rest of
``tests/api`` skips itself -- there is no DSN to be missing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from agent_workbench.apps.api.routes import tasks as tasks_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.bootstrap.projections import MultiAgentConfig
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


class _StubPrincipals:
    def __init__(self) -> None:
        self.resolved = 0

    def resolve(self, request: object) -> object:
        self.resolved += 1
        return PRINCIPAL


class _RecordingTaskService:
    """Records what it was asked for, so a mis-routed literal path is visible.

    It answers nothing: every test here either does not reach a task route, or
    is asserting that it did not.
    """

    def __init__(self) -> None:
        self.asked_for: list[str] = []

    async def get(self, _principal: object, task_id: str) -> Any:
        self.asked_for.append(task_id)
        raise AssertionError(f"the task route ran for {task_id!r}")


def _multi_agent(
    *,
    delegation_enabled: bool,
    max_delegation_depth: int = 1,
    max_children_per_run: int = 4,
    max_parallel_child_invocations: int = 2,
    max_tokens_per_agent_invocation: int = 120_000,
) -> MultiAgentConfig:
    return MultiAgentConfig(
        static_agent_node_limit=6,
        max_parallel_agent_invocations=3,
        max_tokens_per_agent_invocation=max_tokens_per_agent_invocation,
        max_cost_micro_usd_per_agent_invocation=None,
        max_seconds_per_agent_invocation=None,
        delegation_enabled=delegation_enabled,
        max_delegation_depth=max_delegation_depth,
        max_children_per_run=max_children_per_run,
        max_parallel_child_invocations=max_parallel_child_invocations,
    )


def _get(
    multi_agent: MultiAgentConfig | None,
    path: str = "/v1/tasks/capabilities",
) -> tuple[httpx.Response, _RecordingTaskService]:
    app = FastAPI()
    app.include_router(tasks_route.router)
    service = _RecordingTaskService()
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(
            principals=_StubPrincipals(),
            config=SimpleNamespace(multi_agent=multi_agent),
            task_service=service,
        ),
    )

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.get(path, headers=HEADERS)

    return asyncio.run(execute()), service


def test_a_delegating_deployment_reports_the_ceilings_it_would_apply() -> None:
    """The numbers differ per profile, which is the whole reason to ask.

    ``config.default.toml`` ships delegation off with four children;
    ``config.demo-local.toml`` and ``config.code-local.toml`` ship it on with
    six. A submission form that hard-coded either would be describing some
    other deployment on two of the three.
    """

    response, _ = _get(
        _multi_agent(
            delegation_enabled=True,
            max_children_per_run=6,
            max_parallel_child_invocations=2,
            max_tokens_per_agent_invocation=120_000,
        )
    )

    assert response.status_code == 200
    assert response.json() == {
        "delegation": {
            "enabled": True,
            "max_delegation_depth": 1,
            "max_children_per_run": 6,
            "max_parallel_child_invocations": 2,
            "max_tokens_per_agent_invocation": 120_000,
        }
    }


def test_delegation_off_and_a_projection_that_cannot_say_answer_alike() -> None:
    """Two causes, one shape, and the folding is the decision.

    A projection that predates ADR-089 and a deployment that turned delegation
    off are the same answer to the only question this endpoint is asked. A
    third state would be one a console had to invent a rendering for, and the
    honest rendering of both is the same sentence: the next Task here will not
    delegate.
    """

    off, _ = _get(_multi_agent(delegation_enabled=False))
    absent, _ = _get(None)

    assert off.json() == absent.json()
    assert off.json()["delegation"]["enabled"] is False
    # Not zero: these describe a tree that is not built, not one that has run
    # out of room. Zero children reads as a delegating deployment at its limit.
    assert off.json()["delegation"]["max_children_per_run"] == 1
    assert off.json()["delegation"]["max_delegation_depth"] == 1
    # Zero here, because with no invocation there is nothing to bound.
    assert off.json()["delegation"]["max_tokens_per_agent_invocation"] == 0


def test_the_ceilings_of_a_deployment_that_delegates_are_not_the_disabled_ones() -> (
    None
):
    """The control for the test above.

    Without it, a bug that answered the disabled shape unconditionally would
    pass both of the other tests' equality checks against each other.
    """

    on, _ = _get(_multi_agent(delegation_enabled=True, max_children_per_run=4))
    off, _ = _get(_multi_agent(delegation_enabled=False, max_children_per_run=4))

    assert on.json() != off.json()


def test_capabilities_is_not_read_as_a_task_id() -> None:
    """The trap ``/triage`` documents, on a second literal path.

    FastAPI matches in declaration order, so a literal route declared below
    ``/{task_id}`` is a Task whose id happens to be a word. The service here
    raises if it is ever asked, so a regression is a 500 rather than a silently
    plausible 404.
    """

    response, service = _get(_multi_agent(delegation_enabled=True))

    assert response.status_code == 200
    assert service.asked_for == []


def test_which_sub_agents_exist_is_deliberately_not_answered_here() -> None:
    """This process holds the Code catalogue, not the Task one.

    ``CODE_SUB_AGENTS`` is what an API process assembles (``explorer`` and
    ``analyst``); a Task Worker assembles ``DEFAULT_SUB_AGENTS``
    (``researcher`` and ``analyst``), then narrows it to the tools that
    process actually registered. Naming either here would be this process
    answering a question about another one, and the failure mode is the one
    the computer page refuses in the same words: a plausible list is read as
    the real one.

    Pinned as an absence because that is the whole claim. If a later change
    wants to answer it, the answer has to come from somewhere that knows.
    """

    body = _get(_multi_agent(delegation_enabled=True))[0].json()

    assert set(body) == {"delegation"}
    serialized = str(body)
    for name in ("researcher", "analyst", "explorer", "sub_agent", "catalogue"):
        assert name not in serialized
