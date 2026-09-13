"""A person's input, forwarded into the guarded browser (ADR-0117).

The second browser route, in its own module so that ``routes/browser.py`` --
the read-only forward ADR-0113 §3.6 argued for, and the one
``tests/architecture/test_browser_forward_is_read_only.py`` pins -- stays a
read. This module is the deliberate widening that test exists to make loud,
and it is narrow in a different way: **one tool, and a rule about when.**

* One tool: everything here becomes a ``browser_interact`` call and nothing
  else. No ``browser_open`` (the person does not choose what page the model is
  working on), no ``browser_eval`` (a person typing JavaScript into the
  model's page is a console, not a panel).
* A rule about when: ADR-0113 §4 refused this because two operators on one
  page need an arbitration story. The story is the simplest one that is true:
  **the model has the page while a turn is running, and the person has it
  otherwise.** A click that arrives mid-turn is refused with 409 and a
  sentence, not queued and not interleaved -- the model's next snapshot would
  otherwise describe a page somebody else had just changed.

Why this exists at all: the panel beside a coding session showed the page the
model had opened and said 「点不动它」. A person who has just watched a model
open their game wants to press the arrow keys, and the sandboxed preview in
the same panel can do that for a page that is a file -- but not for the page
the model is actually looking at.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of

BROWSER_INPUT_PREFIX: Final[str] = "/v1/browser"

#: The scope the browser's six tools already demand of a model (ADR-0113): a
#: person driving the same browser holds the same key.
BROWSER_SCOPE: Final[str] = "mcp:browser"

#: How many actions one request may carry. A batch is one gesture -- a click,
#: a key, a scroll -- and a gesture is short; the limit keeps a burst of key
#: repeats from becoming a request the browser spends seconds on.
MAX_ACTIONS: Final[int] = 10

router = APIRouter(prefix=BROWSER_INPUT_PREFIX, tags=["browser"])


class ClickAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["click"]
    #: Viewport CSS pixels, the space the screencast frame is in.
    x: int = Field(ge=0, le=10_000)
    y: int = Field(ge=0, le=10_000)


class KeyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["key", "type"]
    text: str = Field(min_length=1, max_length=2_000)


class ScrollAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scroll"]
    delta_y: int = Field(ge=-20_000, le=20_000)


class BrowserInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: tuple[ClickAction | KeyAction | ScrollAction, ...] = Field(
        min_length=1, max_length=MAX_ACTIONS
    )


class BrowserInputResponse(BaseModel):
    #: One line per action, in order, the browser's own words -- the same
    #: strings the model reads back from `browser_interact`.
    done: tuple[str, ...]


@router.post("/input", response_model=BrowserInputResponse)
async def input_(body: BrowserInputRequest, request: Request) -> BrowserInputResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    if BROWSER_SCOPE not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"driving the browser needs the {BROWSER_SCOPE} scope",
        )
    slot = dependencies.code_browser
    if slot is None or slot.client is None:
        # The same distinction `routes/browser.py` keeps: not configured, or
        # configured and not up, are both "nothing here to drive", and 503 is
        # the code the frame route already uses for the second.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the browser is not running in this deployment",
        )
    code = dependencies.code
    if code is not None and code.turns_in_flight > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the model is driving the browser: "
                f"{code.turns_in_flight} coding turn(s) in flight; "
                "wait for the turn to finish"
            ),
        )
    actions: list[dict[str, Any]] = [
        action.model_dump(exclude_none=True) for action in body.actions
    ]
    done = await slot.interact(actions)
    return BrowserInputResponse(done=tuple(done))


__all__ = ["BROWSER_INPUT_PREFIX", "BROWSER_SCOPE", "MAX_ACTIONS", "router"]
