"""``/v1/settings/provider-key``: what may be said, and what must never be.

No database and no model. This route reads one file and reports two booleans,
so the harness is the router plus a stub identity adapter and a real
``ProviderKeyStore`` over ``tmp_path`` -- which is also why these run in CI
while most of ``tests/api`` skips itself.

The store is real rather than mocked on purpose. Every claim here is about what
actually reaches the filesystem: the mode of the file, the bytes in it, and the
paths the store refuses. A mock would let a route that wrote nothing pass, and
would let a route that wrote the key into the checkout pass too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from agent_workbench.application.provider_key import ProviderKeyStore
from agent_workbench.apps.api.routes import settings as settings_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")

#: Long enough that `fingerprint` shows its last four rather than giving up,
#: and deliberately not key-shaped.
#:
#: The first spelling of these was a name ending in `_KEY` assigned a long
#: `sk-` prefixed string, and the repository's own secret scan flagged both
#: lines -- correctly. (Not quoted here: the literal would trip the same rule
#: from inside the comment explaining it, which it did once already.) Gitleaks
#: reads
#: a high-entropy string assigned to a name containing "key", which is exactly
#: what a real credential pasted into a test looks like; it cannot tell that
#: this one was invented. Allowlisting the file would have been the wrong fix:
#: this is the one file where a real key is most likely to arrive by accident,
#: so it is the last place to make the scanner quieter. The fixture says out
#: loud that it is not a credential instead.
STORED_VALUE = "example-not-a-credential-0001"
OTHER_VALUE = "example-not-a-credential-0002"


class _StubPrincipals:
    def resolve(self, request: object) -> PrincipalContext:
        return PRINCIPAL


def _call(
    method: str,
    *,
    key_file: Path | None,
    checkout_root: Path | None = None,
    active_key: str | None = None,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    app = FastAPI()
    app.include_router(settings_route.router)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(
            principals=_StubPrincipals(),
            config=SimpleNamespace(
                model=SimpleNamespace(
                    api_key=SecretStr(active_key) if active_key else None
                )
            ),
            provider_keys=ProviderKeyStore(
                key_file=key_file, checkout_root=checkout_root
            ),
        ),
    )

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.request(
                method, "/v1/settings/provider-key", headers=HEADERS, json=json
            )

    return asyncio.run(execute())


def test_nothing_stored_and_nothing_running_says_both(tmp_path: Path) -> None:
    body = _call("GET", key_file=tmp_path / "key").json()
    assert body["active"] is False
    assert body["stored"] is False
    assert body["fingerprint"] is None
    assert body["restart_required"] is False


def test_storing_a_key_writes_it_and_says_a_restart_is_owed(tmp_path: Path) -> None:
    """The two halves this route exists to keep apart, in one response."""
    target = tmp_path / "key"
    body = _call("PUT", key_file=target, json={"api_key": f"  {STORED_VALUE}  "}).json()

    assert target.read_text(encoding="utf-8") == STORED_VALUE + "\n"
    assert body["stored"] is True
    # Nothing was composed with it, so it cannot be in effect -- and the page
    # has to say so rather than report a success that changes nothing visible.
    assert body["active"] is False
    assert body["restart_required"] is True
    assert body["restart_hint"]


def test_the_stored_key_is_never_returned(tmp_path: Path) -> None:
    """The one guarantee this route makes without any authentication behind it."""
    target = tmp_path / "key"
    _call("PUT", key_file=target, json={"api_key": STORED_VALUE})
    for method in ("GET", "PUT", "DELETE"):
        response = _call(
            method,
            key_file=target,
            active_key=STORED_VALUE,
            json={"api_key": STORED_VALUE} if method == "PUT" else None,
        )
        assert STORED_VALUE not in response.text
        # Four characters, and they are the last four.
        if response.json().get("fingerprint"):
            assert response.json()["fingerprint"] == "…" + STORED_VALUE[-4:]


def test_storing_the_key_already_running_owes_no_restart(tmp_path: Path) -> None:
    """Otherwise the page sends someone to restart a process that returns identical."""
    target = tmp_path / "key"
    body = _call(
        "PUT", key_file=target, active_key=STORED_VALUE, json={"api_key": STORED_VALUE}
    ).json()
    assert body["stored"] is True
    assert body["active"] is True
    assert body["restart_required"] is False
    assert body["restart_hint"] == ""


def test_a_stored_key_that_differs_from_the_running_one_owes_a_restart(
    tmp_path: Path,
) -> None:
    target = tmp_path / "key"
    body = _call(
        "PUT", key_file=target, active_key=STORED_VALUE, json={"api_key": OTHER_VALUE}
    ).json()
    assert body["restart_required"] is True


def test_the_file_is_not_world_readable(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "key"
    _call("PUT", key_file=target, json={"api_key": STORED_VALUE})
    # Advisory on Windows, which is why the assertion is on the group and other
    # bits rather than on the whole mode: those are the ones that mean something
    # everywhere this runs.
    assert target.stat().st_mode & 0o077 == 0


def test_writing_inside_the_checkout_is_refused(tmp_path: Path) -> None:
    """`zip -r` and Finder's Compress ignore .gitignore. So this cannot be stored."""
    checkout = tmp_path / "repo"
    (checkout / "var").mkdir(parents=True)
    response = _call(
        "PUT",
        key_file=checkout / "var" / "key",
        checkout_root=checkout,
        json={"api_key": STORED_VALUE},
    )
    assert response.status_code == 400
    assert "checkout" in response.json()["detail"]
    assert not (checkout / "var" / "key").exists()


def test_a_deployment_with_no_key_file_refuses_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """`AW_KEY_FILE=""` is a deployment saying the console may not store one."""
    response = _call("PUT", key_file=None, json={"api_key": STORED_VALUE})
    assert response.status_code == 400
    assert response.json()["detail"]


def test_an_empty_key_is_refused_before_it_reaches_the_disk(tmp_path: Path) -> None:
    target = tmp_path / "key"
    # 422 from the model's own min_length, which is the earliest place to say no.
    assert _call("PUT", key_file=target, json={"api_key": ""}).status_code == 422
    # And whitespace that survives that check still does not become a key.
    assert _call("PUT", key_file=target, json={"api_key": "   "}).status_code == 400
    assert not target.exists()


def test_an_unknown_field_in_the_body_is_refused(tmp_path: Path) -> None:
    """`extra="forbid"`: nothing rides alongside the key into this handler."""
    response = _call(
        "PUT",
        key_file=tmp_path / "key",
        json={"api_key": STORED_VALUE, "tenant_id": "somebody_else"},
    )
    assert response.status_code == 422


def test_clearing_removes_the_file_and_leaves_the_running_process_alone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "key"
    _call("PUT", key_file=target, json={"api_key": STORED_VALUE})
    body = _call("DELETE", key_file=target, active_key=STORED_VALUE).json()
    assert not target.exists()
    assert body["stored"] is False
    # The process keeps the key it was composed with; clearing the file is
    # about the next start, and saying otherwise would be a second lie in the
    # same field.
    assert body["active"] is True


def test_clearing_nothing_is_not_an_error(tmp_path: Path) -> None:
    assert _call("DELETE", key_file=tmp_path / "absent").status_code == 200
