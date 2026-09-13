"""A person's input into the guarded browser (ADR-0117, ADR-0119).

Two refusals and one forward. The third refusal -- no coding turn driving the
page -- is gone (ADR-0119): it made the panel refuse at the one moment it was
wanted, since the turn that opens a page is the turn that keeps it open for
minutes. What replaced it is a fact travelling both ways: the person is told
the model is working on this page too, and the model is told, on its next
browser call, that a person touched it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from agent_workbench.apps.api.routes import browser_input as route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}


class _Principals:
    def __init__(self, scopes: tuple[str, ...]) -> None:
        self._scopes = scopes

    def resolve(self, request: object) -> PrincipalContext:
        del request
        return PrincipalContext(
            principal_id="user_1", tenant_id="tenant_a", scopes=self._scopes
        )


class _Slot:
    """The browser slot as the route sees it: a client, and `interact`."""

    def __init__(self, *, opened: bool = True) -> None:
        self.client = object() if opened else None
        self.forwarded: list[list[dict[str, Any]]] = []

    async def interact(self, actions: list[dict[str, Any]]) -> list[str]:
        self.forwarded.append(actions)
        return [
            f"action {index} ({action['kind']}) ok"
            for index, action in enumerate(actions)
        ]


def _dependencies(
    *,
    scopes: tuple[str, ...] = ("mcp:browser",),
    slot: _Slot | None = None,
    turns: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        principals=_Principals(scopes),
        code_browser=slot,
        code=SimpleNamespace(turns_in_flight=turns),
    )


def _post(dependencies: SimpleNamespace, body: dict[str, Any]) -> httpx.Response:
    app = FastAPI()
    app.include_router(route.router)
    setattr(app.state, STATE_ATTRIBUTE, dependencies)

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.post("/v1/browser/input", json=body, headers=HEADERS)

    return asyncio.run(execute())


def test_a_click_is_forwarded_as_one_interact_call() -> None:
    slot = _Slot()

    answered = _post(
        _dependencies(slot=slot),
        {
            "actions": [
                {"kind": "click", "x": 640, "y": 300},
                {"kind": "key", "text": "Enter"},
            ]
        },
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["done"] == ["action 0 (click) ok", "action 1 (key) ok"]
    assert slot.forwarded == [
        [{"kind": "click", "x": 640, "y": 300}, {"kind": "key", "text": "Enter"}]
    ]


def test_a_turn_in_flight_no_longer_refuses_it_reports() -> None:
    """ADR-0119 replaced the lock with a fact, and this is the reversal.

    The previous version of this test asserted 409 and an empty forward. A
    turn that writes a page and then verifies it in the browser runs for
    minutes, and those are exactly the minutes a person wants to press the
    arrow keys in -- so the rule refused every time it mattered and never
    when it did not. Now the input lands and the count comes back, for the
    panel to say "the model is working on this page too".
    """

    slot = _Slot()

    answered = _post(
        _dependencies(slot=slot, turns=2),
        {"actions": [{"kind": "click", "x": 1, "y": 1}]},
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["turns_in_flight"] == 2
    assert slot.forwarded == [[{"kind": "click", "x": 1, "y": 1}]]


def test_a_quiet_process_reports_no_turns() -> None:
    """The control: the same field, and it is not a constant."""

    answered = _post(
        _dependencies(slot=_Slot()), {"actions": [{"kind": "key", "text": "a"}]}
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["turns_in_flight"] == 0


def test_no_browser_is_503_not_404() -> None:
    absent = _post(
        _dependencies(slot=None), {"actions": [{"kind": "key", "text": "a"}]}
    )
    closed = _post(
        _dependencies(slot=_Slot(opened=False)),
        {"actions": [{"kind": "key", "text": "a"}]},
    )

    assert absent.status_code == 503
    assert closed.status_code == 503


def test_driving_the_browser_needs_the_scope_the_model_needs() -> None:
    slot = _Slot()

    answered = _post(
        _dependencies(slot=slot, scopes=()),
        {"actions": [{"kind": "click", "x": 1, "y": 1}]},
    )

    assert answered.status_code == 403
    assert slot.forwarded == []


def test_only_the_four_gestures_are_accepted() -> None:
    """No `open`, no `eval`: a person points and presses, nothing else."""

    slot = _Slot()

    opened = _post(
        _dependencies(slot=slot),
        {"actions": [{"kind": "open", "url": "https://example.com"}]},
    )
    evaluated = _post(
        _dependencies(slot=slot),
        {"actions": [{"kind": "eval", "expression": "1+1"}]},
    )
    too_many = _post(
        _dependencies(slot=slot),
        {"actions": [{"kind": "key", "text": "a"}] * (route.MAX_ACTIONS + 1)},
    )

    assert opened.status_code == 422
    assert evaluated.status_code == 422
    assert too_many.status_code == 422
    assert slot.forwarded == []
