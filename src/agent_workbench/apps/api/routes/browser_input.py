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
* A rule about when -- which, since ADR-0119, is **no rule at all**. ADR-0117
  arbitrated by refusing a person's click with 409 while any coding turn was
  running, and a week of use said what that is worth: a turn writing a page
  and then verifying it in the browser runs for minutes, so the panel was
  never usable at the one moment somebody wants it, which is while the model
  has the page open. The arbitration is now the one Claude Code uses for its
  own Browser pane: both may drive, and the model is *told* -- its next
  browser call carries a line saying a person touched the page and that its
  snapshot refs may no longer mean anything. A fact it can act on beats a
  lock it has to wait out.

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
    #: How many coding turns are running as this input lands (ADR-0119). Not a
    #: refusal any more, a fact: the panel says "the model is working on this
    #: page too" so a person who is surprised by the page moving knows why.
    turns_in_flight: int = 0


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
    actions: list[dict[str, Any]] = [
        action.model_dump(exclude_none=True) for action in body.actions
    ]
    done = await slot.interact(actions)
    # Told, not refused (ADR-0119). The count is the same fact the 409 used to
    # carry and the person is the same person; what changed is who decides
    # what to do about it. They can watch the model work and then take the
    # keyboard, or press a key while it thinks -- and the model is told, on
    # its next browser call, that the page moved.
    return BrowserInputResponse(
        done=tuple(done),
        turns_in_flight=code.turns_in_flight if code is not None else 0,
    )


__all__ = ["BROWSER_INPUT_PREFIX", "BROWSER_SCOPE", "MAX_ACTIONS", "router"]
