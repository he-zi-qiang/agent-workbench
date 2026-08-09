"""The checked-in local profile for the read-only web MCP server (ADR-027).

Every assertion here is paired with the ordinary local profile, which must stay
unchanged. A profile that quietly enabled this would widen every Task submitted
by anybody using the default local setup -- the tool names are frozen into each
new Task's authorization envelope, and that envelope is re-applied on resume.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_workbench.bootstrap.projections import project_task, project_task_worker
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / "config/config.local.toml"
WEB_CONFIG = ROOT / "config/config.web-local.toml"
WORD_CONFIG = ROOT / "config/config.word-local.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:local-profile-test@127.0.0.1:5433/agent_workbench_local"
)


def _load_profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    return load_settings(config_file=path)


def test_the_ordinary_local_profile_does_not_widen_tasks_with_the_web_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, LOCAL_CONFIG)

    assert settings.optional_labs.mcp_adapter is False
    assert settings.mcp.servers == ()
    allowed = project_task(settings).default_authorization_envelope.allowed_tools
    assert "mcp_web_fetch_page" not in allowed
    assert "mcp_web_download_document" not in allowed


def test_the_explicit_web_profile_enables_only_the_two_read_only_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, WEB_CONFIG)

    assert settings.optional_labs.mcp_adapter is True
    assert len(settings.mcp.servers) == 1
    server = settings.mcp.servers[0]
    assert server.alias == "web"
    assert server.transport == "http"
    assert server.endpoint == "http://127.0.0.1:8767/mcp"
    assert server.tools == ("fetch_page", "download_document")
    assert server.retryable_effects is True
    # The whole point of this profile: these go to the researcher, not the
    # writer (ADR-027 §3.3).
    assert server.audience == "research"
    assert settings.model.main.tool_calling_required is False

    allowed = project_task(settings).default_authorization_envelope.allowed_tools
    assert "mcp_web_fetch_page" in allowed
    assert "mcp_web_download_document" in allowed


def test_the_two_explicit_profiles_stay_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One combined profile would widen every Task by both capabilities.

    Asserted in both directions because either alone would be satisfied by a
    profile that enabled nothing at all.
    """

    web = _load_profile(monkeypatch, WEB_CONFIG)
    word = _load_profile(monkeypatch, WORD_CONFIG)

    assert [server.alias for server in web.mcp.servers] == ["web"]
    assert [server.alias for server in word.mcp.servers] == ["word"]
    # The Word server keeps the audience it had before the field existed.
    assert word.mcp.servers[0].audience == "synthesis"


def test_the_web_profile_points_the_worker_at_the_server_it_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runbook, the config and the server's own default port agree.

    Three places state 8767, and a Worker pointed at a port nothing serves
    fails soft -- it starts without the tools and says so in a log line nobody
    is watching. That is the failure this pins.
    """

    from agent_workbench.apps.web_mcp.main import DEFAULT_PORT

    worker = project_task_worker(_load_profile(monkeypatch, WEB_CONFIG))

    assert worker.mcp is not None
    assert worker.mcp.servers[0].endpoint == f"http://127.0.0.1:{DEFAULT_PORT}/mcp"
    assert worker.mcp.servers[0].audience == "research"
    runbook = (ROOT / "docs/web-mcp-local.md").read_text(encoding="utf-8")
    assert f"127.0.0.1:{DEFAULT_PORT}" in runbook


def _dev(
    command: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/dev.sh", command],
        cwd=ROOT,
        env={**os.environ, "PYTHON": "/bin/echo", **(environment or {})},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_dev_script_starts_the_project_owned_web_mcp_module() -> None:
    result = _dev("web-server")

    assert result.returncode == 0
    assert result.stdout.strip() == "-m agent_workbench.apps.web_mcp.main"


def test_dev_script_probes_both_tools_not_just_one() -> None:
    """A directory carrying only `fetch_page` is a half-deployed server.

    Checking one tool would report it healthy, and the failure would surface
    later as a download the researcher cannot perform.
    """

    result = _dev("web-check")

    assert result.returncode == 0
    assert result.stdout.split() == [
        "scripts/smoke_mcp_server.py",
        "--label",
        "web",
        "--endpoint",
        "http://127.0.0.1:8767/mcp",
        "--health-url",
        "http://127.0.0.1:8767/health",
        "--expect-tool",
        "fetch_page",
        "--expect-tool",
        "download_document",
    ]


def test_web_worker_refuses_to_fake_the_task_path_without_a_provider() -> None:
    """A demo graph calls no model, so it proposes no tool.

    "It ran" would prove the process starts and nothing about the capability.
    """

    environment = dict(os.environ)
    environment.pop("AW_SECRETS__DEEPSEEK_API_KEY", None)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "web-worker"],
        cwd=ROOT,
        env={**environment, "PYTHON": "/bin/echo"},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "requires AW_SECRETS__DEEPSEEK_API_KEY" in result.stderr


def test_web_worker_uses_the_real_graph_and_explicit_profile_with_a_provider(
    tmp_path: Path,
) -> None:
    """The exported profile is the assertion, not just the module that runs.

    A `$PYTHON` that only echoes its arguments cannot show which config file the
    arm selected, and selecting the wrong one is precisely the mistake worth
    catching: the Worker would start, register no web tool, and say so in a log
    line nobody is watching.
    """

    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "web-worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": str(probe),
            "AW_SECRETS__DEEPSEEK_API_KEY": "contract-only-not-a-real-key",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "config/config.web-local.toml",
        "-m agent_workbench.apps.task_worker.main",
    ]
    # The probe runs before the Worker, so a missing server is a refusal to
    # start rather than a Worker that quietly comes up without the tools.
    assert "scripts/smoke_mcp_server.py --label web --endpoint" in result.stderr


def test_the_usage_banner_lists_every_command_the_case_arm_answers() -> None:
    """The banner is a `sed` range over this file's own header.

    Adding a command without extending the range leaves it undocumented, and
    the range is exactly the kind of thing that is never noticed by hand.
    """

    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    documented = {
        line.split()[2]
        for line in script.splitlines()
        if line.startswith("#   scripts/dev.sh ")
    }
    banner = _dev("")

    for command in ("web-server", "web-check", "web-api", "web-worker"):
        assert command in documented
        assert command in banner.stdout
