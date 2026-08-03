"""Serving the console next to the API, and the two ways that goes wrong.

The mount is what removes CORS from the picture, so the properties worth
pinning are the ones a same-origin console depends on: an API path is never
answered by a file, and a deployment that was told to serve a console either
serves one or refuses to start.

The React console is built by the frontend gate. What is under test here is the
framework-neutral assembly contract, using a tiny static fixture so Python
tests do not secretly depend on a prior Node build.
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_workbench.apps.api.main import build_app
from agent_workbench.apps.api.web import (
    BUILD_MARKER,
    ENTRY_FILE,
    WEB_PREFIX,
    WebDirectoryError,
    resolve_web_directory,
)
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings

SOURCE_CONSOLE = Path(__file__).resolve().parents[2] / "web"


#: Never connected to. These tests are about routing and mounting, so the
#: application is built but its lifespan is deliberately not entered -- nothing
#: here touches a route that reaches a store.
UNUSED_DSN = "postgresql+asyncpg://unit:test@127.0.0.1:1/agent_workbench"


def _settings(root: Path) -> Settings:
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    payload["database"].update(
        dsn=UNUSED_DSN, guard_dsn=UNUSED_DSN, listen_dsn=UNUSED_DSN
    )
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return Settings(**payload)


def _client(root: Path, web_directory: Path | None) -> TestClient:
    """A client that never enters the application lifespan.

    Startup opens connections and warms an encoder; none of that is what these
    tests are about, and requiring it would make a routing test need a
    database.
    """

    app, _ = build_app(
        project_api(_settings(root)), with_chat=False, web_directory=web_directory
    )
    return TestClient(app)  # pyright: ignore[reportArgumentType]


@pytest.fixture
def console(tmp_path: Path) -> Path:
    """A built-console-shaped fixture independent of Vite's output directory."""

    directory = tmp_path / "console"
    assets = directory / "assets"
    assets.mkdir(parents=True)
    (directory / ENTRY_FILE).write_text(
        """<!doctype html><html><head><title>Agent Workbench</title>\
<link rel=\"stylesheet\" href=\"./assets/app.css\"></head>\
<body><div id=\"root\"></div><script type=\"module\" \
src=\"./assets/app.js\"></script></body></html>""",
        encoding="utf-8",
    )
    (directory / BUILD_MARKER).write_text(
        '{"artifact":"vite-production-build","schema_version":1}',
        encoding="utf-8",
    )
    (assets / "app.js").write_text(
        "document.title = 'Agent Workbench'", encoding="utf-8"
    )
    (assets / "app.css").write_text("body { margin: 0; }", encoding="utf-8")
    return directory


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_a_missing_console_directory_refuses_at_startup(tmp_path: Path) -> None:
    """A process told to serve a console and serving nothing looks healthy.

    The person who mistyped the path would find out from a browser, so the
    check happens where a failure is still a startup failure.
    """

    with pytest.raises(WebDirectoryError):
        resolve_web_directory(str(tmp_path / "absent"))


def test_a_directory_without_an_entry_file_is_not_a_console(tmp_path: Path) -> None:
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")

    with pytest.raises(WebDirectoryError):
        resolve_web_directory(str(tmp_path))


def test_the_source_tree_is_not_accepted_as_a_built_console() -> None:
    """Serving Vite source would return TSX instead of executable assets."""

    with pytest.raises(WebDirectoryError, match="production build"):
        resolve_web_directory(str(SOURCE_CONSOLE))


# --------------------------------------------------------------------------
# What is served, and what is not
# --------------------------------------------------------------------------


def test_the_console_is_served_under_its_prefix(tmp_path: Path, console: Path) -> None:
    client = _client(tmp_path, console)
    response = client.get(f"{WEB_PREFIX}/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Agent Workbench" in response.text


def test_the_root_redirects_rather_than_serving_a_second_copy(
    tmp_path: Path, console: Path
) -> None:
    """Two addresses for one page means relative asset paths resolve at one."""

    client = _client(tmp_path, console)
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"{WEB_PREFIX}/"


def test_the_console_assets_are_reachable(tmp_path: Path, console: Path) -> None:
    client = _client(tmp_path, console)
    script = client.get(f"{WEB_PREFIX}/assets/app.js")
    stylesheet = client.get(f"{WEB_PREFIX}/assets/app.css")

    assert script.status_code == 200
    assert stylesheet.status_code == 200
    # The page loads these by relative path; a rename here breaks the console
    # and nothing else would notice.
    entry = (console / ENTRY_FILE).read_text(encoding="utf-8")
    assert "./assets/app.js" in entry
    assert "./assets/app.css" in entry


def test_an_api_path_is_never_answered_by_a_file(tmp_path: Path, console: Path) -> None:
    """A static mount that shadowed a route would turn a mistyped API path into
    a page of HTML with a 200 on it, and the client that asked for JSON would
    have to parse a document to discover it was wrong."""

    client = _client(tmp_path, console)
    live = client.get("/health/live")
    unauthenticated = client.get("/v1/tasks")
    unknown = client.get("/v1/nothing-here")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    # Still the API's own refusal, not an index page.
    assert unauthenticated.status_code == 401
    assert unknown.status_code == 404
    assert "<html" not in unknown.text.lower()


def test_without_a_directory_no_console_is_mounted(tmp_path: Path) -> None:
    """The same answer this codebase gives elsewhere: a surface that cannot
    work is not registered, so a client meets one 404 rather than a page that
    loads and then fails at every request."""

    client = _client(tmp_path, None)
    console = client.get(f"{WEB_PREFIX}/")
    root = client.get("/", follow_redirects=False)

    assert console.status_code == 404
    assert root.status_code == 404


# --------------------------------------------------------------------------
# What the console is allowed to depend on
# --------------------------------------------------------------------------


def test_the_console_has_no_external_dependency(console: Path) -> None:
    """The built console must not depend on a CDN at browser runtime.

    A console that fetched a script from somewhere else would put a third party
    on the path of a page holding the operator's identity headers -- and would
    stop working on the loopback-only deployment this is built for.
    """

    for path in console.rglob("*"):
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        assert "http://" not in source
        assert "https://" not in source
        assert "cdn" not in source.lower()


def test_a_transport_probe_reaches_the_console_over_asgi(
    tmp_path: Path, console: Path
) -> None:
    """One request through the real stack, including the size limiter.

    The console is served through ``ControlPlaneLimit`` like everything else;
    a GET carries no body, so the limiter must pass it through untouched.
    """

    app, _ = build_app(
        project_api(_settings(tmp_path)), with_chat=False, web_directory=console
    )
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]

    async def probe() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.get(f"{WEB_PREFIX}/{ENTRY_FILE}")

    response = asyncio.run(probe())

    assert response.status_code == 200
    assert "Agent Workbench" in response.text
