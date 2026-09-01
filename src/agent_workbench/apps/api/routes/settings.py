"""The one place this control plane accepts a secret (ADR-101).

**Three routes, one value, and the narrowness is the decision.** Until this file
existed no endpoint in this process wrote configuration at all, let alone a
credential -- so the honest framing is not "a settings page" but "the control
plane gained a write to a file outside the checkout", and ADR-101 records what
that costs and under which premise it is acceptable.

The premise is the one ADR-044 already states: the Identity Adapter trusts
request headers, the API binds loopback, and this is controlled local
development rather than a deployment. Anything that can reach this port can set
this key. That is not mitigated here and pretending otherwise would be worse
than saying it: the route resolves a principal like every other route, and that
resolution is a shape, not a defence.

What *is* defended, because it is defensible without authentication:

* **The key never comes back.** ``GET`` returns four characters and a boolean.
  There is no method that returns more, because a read-back endpoint is what
  turns "reached the port" into "has the key", and the cheapest way not to have
  one is not to write one.
* **The key never reaches a log.** It arrives in a request body rather than a
  path or a query string, so nothing that records URLs records it.
* **The key cannot land inside the checkout.** ``ProviderKeyStore`` refuses,
  because archiving tools ignore ``.gitignore``.

And one thing this route deliberately cannot do: make a stored key take effect.
The model client is built once, at composition, and the chat routes are mounted
only when that build found a key -- so ``restart_required`` is a real field with
a real answer, not a caveat in a docstring. A page that reported "saved" and
left someone wondering why chat was still missing would be the failure this
whole file is trying not to be.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.application.provider_key import ProviderKeyRefused
from agent_workbench.apps.api.state import dependencies_of

SETTINGS_PREFIX: Final[str] = "/v1/settings"

#: Long enough for any provider's key, short enough that a paste accident is
#: refused here rather than written to disk and refused by the provider later.
MAX_KEY_LENGTH: Final[int] = 512

router = APIRouter(prefix=SETTINGS_PREFIX, tags=["settings"])


class ProviderKeyView(BaseModel):
    """What may be said out loud about a key, and nothing beyond it."""

    #: Whether *this process* was composed with a key. The only one that can
    #: answer a question right now.
    active: bool
    #: Whether a key is on disk for the next start. Separate from ``active`` on
    #: purpose -- collapsing them is how a settings page comes to claim that a
    #: key it saved a second ago is already working.
    stored: bool
    #: The last four characters. Never more, and never the key.
    fingerprint: str | None = None
    #: Where a stored key lives, folded to `~`. ``None`` when this deployment
    #: declared no key file, which is a state and not a missing value.
    path: str | None = None
    #: True when a stored key differs from the running one, so the difference
    #: cannot show up until something restarts.
    restart_required: bool = False
    #: What to restart, in the console's own words. Empty when nothing is owed.
    restart_hint: str = ""


class ProviderKeyRequest(BaseModel):
    """The key, and nothing else that could be smuggled alongside it."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=MAX_KEY_LENGTH)


def _view(request: Request) -> ProviderKeyView:
    dependencies = dependencies_of(request)
    active = dependencies.config.model.api_key
    status_ = dependencies.provider_keys.status(
        active_key=active.get_secret_value() if active is not None else None
    )
    return ProviderKeyView(
        active=status_.active,
        stored=status_.stored,
        fingerprint=status_.fingerprint,
        path=status_.path,
        restart_required=status_.restart_required,
        restart_hint=(
            "重启 agent-api 与 agent-task-worker 后这把 key 才会生效。"
            if status_.restart_required
            else ""
        ),
    )


@router.get("/provider-key", response_model=ProviderKeyView)
async def read_provider_key(request: Request) -> ProviderKeyView:
    """Report what is stored and what is running, without returning either."""

    # Resolved and discarded, like `computer.session` and `tasks.capabilities`.
    # Nothing here belongs to a principal -- it describes this machine -- but a
    # route reachable without the identity adapter would be the one such route
    # in the process.
    dependencies_of(request).principals.resolve(request)
    return _view(request)


@router.put("/provider-key", response_model=ProviderKeyView)
async def store_provider_key(
    request: Request, body: ProviderKeyRequest
) -> ProviderKeyView | JSONResponse:
    """Store the key for the next start."""

    dependencies = dependencies_of(request)
    dependencies.principals.resolve(request)
    try:
        dependencies.provider_keys.store(body.api_key)
    except ProviderKeyRefused as refused:
        # 400 rather than 500: every refusal this raises is about the request or
        # about a deployment choice the caller can see, and each one already
        # says which rule it was in a sentence a person reads.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(refused)}
        )
    return _view(request)


@router.delete("/provider-key", response_model=ProviderKeyView)
async def clear_provider_key(request: Request) -> ProviderKeyView | JSONResponse:
    """Remove the stored key. The running process keeps the one it has."""

    dependencies = dependencies_of(request)
    dependencies.principals.resolve(request)
    try:
        dependencies.provider_keys.clear()
    except ProviderKeyRefused as refused:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(refused)}
        )
    return _view(request)


__all__ = [
    "MAX_KEY_LENGTH",
    "SETTINGS_PREFIX",
    "ProviderKeyRequest",
    "ProviderKeyView",
    "router",
]
