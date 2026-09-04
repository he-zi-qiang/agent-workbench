"""The profile the Compose stack runs, and the two ways it could be wrong.

`config.compose-local.toml` exists because nothing was choosing: no service in
`compose.yaml` named a profile, so eleven healthy containers presented a
console assembled from `config.default.toml` -- no MCP servers, no triage, no
Code, no delegation. Those are defaults, not a decision about this deployment.

The assertions below are the two mistakes available to whoever edits it next,
and both are mistakes of *copying*. `config.demo-local.toml` describes the same
product on a different machine, so it is the obvious file to start from, and
two of its lines are wrong here in ways that fail far from the edit:

* `code.sandbox_enabled = true` -- `SandboxSession.open` is fail-fast, so the
  API refuses to start. Nothing in a `read_only`, `cap_drop: ALL` topology can
  answer a sandbox probe.
* `[qdrant] url = "http://localhost:6333"` -- correct there, because those
  processes are on the host. Here the shipped default is already the service
  name, and the override sends every container at its own loopback.

Nothing else enumerates the profiles. Ten `config.*.toml` files are loaded by
hand-written constants in separate places, `agent-config-check --profile`
accepts three names, and neither globs the directory -- so a profile with no
test naming it has no gate at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_workbench.apps.web_mcp.main import DEFAULT_PORT as WEB_PORT
from agent_workbench.apps.word_mcp.main import DEFAULT_PORT as WORD_PORT
from agent_workbench.bootstrap.projections import project_task_worker
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CONFIG = ROOT / "config/config.compose-local.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:profile-test@127.0.0.1:5433/agent_workbench_local"
)


def _load(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    return load_settings(config_file=COMPOSE_CONFIG)


def test_the_compose_profile_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every section is `extra="forbid"`, so a mistyped key fails here."""

    settings = _load(monkeypatch)
    assert settings.app.deployment_scope == "local"
    assert settings.optional_labs.mcp_adapter is True


def test_the_compose_profile_carries_both_loopback_mcp_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoints are the sidecars' own defaults, taken from their code.

    `docker/run-task-worker-local.sh` starts both inside the Worker container
    and passes no `--host`, so the day either default port moves, this fails
    rather than the Worker coming up healthy with one tool fewer.
    """

    worker = project_task_worker(_load(monkeypatch))
    assert worker.mcp is not None
    endpoints = {server.alias: server.endpoint for server in worker.mcp.servers}
    assert endpoints == {
        "word": f"http://127.0.0.1:{WORD_PORT}/mcp",
        "web": f"http://127.0.0.1:{WEB_PORT}/mcp",
    }


def test_the_compose_profile_leaves_the_sandbox_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying this line across from `config.demo-local.toml` stops the API.

    Not a degraded start: `SandboxSession.open` raises `SandboxUnavailableError`
    at boot, on the argument ADR-057 §3 made -- a session told in configuration
    that it may run code, which then fails per call, is worse than one that
    never offered.
    """

    settings = _load(monkeypatch)
    assert settings.code.sandbox_enabled is False
    assert settings.sandbox.enabled is False


def test_the_compose_profile_leaves_qdrant_where_the_container_network_puts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default is the service name. An override would be loopback."""

    settings = _load(monkeypatch)
    assert settings.qdrant.url == "http://qdrant:6333"


def test_the_compose_profile_asks_the_encoder_for_its_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process loads the weights; the other four name it (ADR-0106).

    Both leaves, because each factory reads only its own section, and the
    service name rather than a loopback address: unlike an MCP server the
    encoder is its own container, reached the way Qdrant is.
    """

    settings = _load(monkeypatch)
    assert settings.rag.embedding.service_url == "http://encoder:8769"
    assert settings.rag.reranker.service_url == "http://encoder:8769"

    worker = project_task_worker(settings)
    assert worker.embedding is not None
    assert worker.embedding.service_url == "http://encoder:8769"


def test_the_compose_profile_leaves_the_sandbox_to_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off in the file, decided per start (ADR-0107).

    The topology now has a broker, and `true` here is still the value that
    would stop a stack whose broker is pulling its image -- so the file says
    nothing and the launcher exports `AW_SANDBOX__ENABLED` /
    `AW_CODE__SANDBOX_ENABLED` when the runtime answers. The endpoint the
    export would name is the shipped loopback default, which is what each
    container's tunnel listens on.
    """

    settings = _load(monkeypatch)
    assert settings.code.sandbox_enabled is False
    assert settings.sandbox.enabled is False
    assert settings.sandbox.endpoint == "http://127.0.0.1:8766/mcp"

    monkeypatch.setenv("AW_SANDBOX__ENABLED", "true")
    monkeypatch.setenv("AW_CODE__SANDBOX_ENABLED", "true")
    decided = load_settings(config_file=COMPOSE_CONFIG)
    assert decided.sandbox.enabled is True
    assert decided.code.sandbox_enabled is True
    assert project_task_worker(decided).sandbox is not None
