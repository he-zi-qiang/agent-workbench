"""The local walkthrough, and the two ways it described the wrong deployment.

`scripts/smoke_local.py` is the rehearsal tool: it is what gets run before a
console is demonstrated, so what it prints is read as a statement about the
running system. It was written against `config.local.toml` and carried two
assumptions from there that are false against the console profile -- it sent no
principal scopes, and it ended by asserting that no chat route was served.

Neither showed up as an error. The Task settled `succeeded` with every one of
its seven tool calls denied, and the closing line printed in green.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts/smoke_local.py"
CONSOLE_IDENTITY = ROOT / "web/src/app/IdentityContext.tsx"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_local", SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    return _load()


def _console_scopes() -> list[str]:
    """The scope list the browser console sends, read out of its own source."""

    source = CONSOLE_IDENTITY.read_text(encoding="utf-8")
    block = re.search(r"scopes:\s*\[(.*?)\]", source, re.DOTALL)
    assert block is not None, "the console's DEFAULT_IDENTITY no longer has scopes"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_the_walkthrough_asks_for_the_scopes_the_console_asks_for(
    smoke: ModuleType,
) -> None:
    """Pinned against the console's own source, not against a copy of the list.

    The two are separate clients of one API and they drifted: the console grew
    `workspace:write` and the two `mcp:*` scopes after a real Task was denied
    `missing_permission_scope` and fell back to pasting its report into the
    chat, and this script never grew them at all. A test that restated the list
    here would agree with whichever side it was written from.
    """

    assert list(smoke.CONSOLE_SCOPES) == _console_scopes()


def test_every_request_carries_the_scopes(smoke: ModuleType) -> None:
    """The header, not just the constant.

    The constant existing and the requests sending it are different facts, and
    the failure this test is about was the second one: the script had a
    principal and a tenant on every call and no scopes on any of them.
    """

    args = argparse.Namespace(
        tenant_id="tenant_local",
        principal_id="user_local",
        scopes=("workspace:write", "mcp:word"),
    )

    headers = smoke._headers(args)

    assert headers["x-principal-scopes"] == "workspace:write,mcp:word"
    # Comma-separated, the same encoding `web/src/api/client.ts` uses. A space
    # separator would read as one long scope and deny everything.
    assert headers["x-tenant-id"] == "tenant_local"
    assert headers["x-principal-id"] == "user_local"


@pytest.mark.parametrize(
    ("paths", "served"),
    [
        ({"/v1/chat/sessions": {}, "/v1/search": {}}, True),
        ({"/v1/search": {}}, False),
    ],
)
def test_the_closing_note_asks_the_deployment_whether_chat_is_served(
    smoke: ModuleType, paths: dict[str, object], served: bool
) -> None:
    """Both directions, because the old sentence was right in one of them.

    "This deployment has no model provider, so the API serves no chat route at
    all" was true of the profile this script was written against, which is
    exactly why it went unnoticed against the profile where the console runs.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json={"paths": paths})

    client = httpx.Client(
        base_url="http://api.test", transport=httpx.MockTransport(handler)
    )
    with client:
        assert smoke._chat_is_served(client) is served


def test_an_api_that_cannot_be_asked_is_not_claimed_to_serve_chat(
    smoke: ModuleType,
) -> None:
    """The conservative direction for a sentence claiming a capability."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.Client(
        base_url="http://api.test", transport=httpx.MockTransport(handler)
    )
    with client:
        assert smoke._chat_is_served(client) is False
