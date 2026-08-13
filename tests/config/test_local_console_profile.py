"""The combined profile the console runs (config.demo-local.toml).

The two narrow profiles each demonstrate one capability and are pinned apart by
`test_local_web_mcp_profile.py`. This one is the union, and it exists because a
console is one application: a Task submitted from Work carries whatever the API
froze into its envelope, and on the web profile a request for a Word document
had no renderer in that envelope at all. What the model did instead is pinned in
`tests/adapters/test_workspace_tools.py` -- it wrote Markdown into a file called
`report.docx`.

So the assertions here are about the union being *complete*: both servers, both
budget corrections, and the export gate a single-machine deployment is allowed
to decline (ADR-038 §2.1). A profile missing any one of them fails in a way that
looks like the model misbehaving.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_workbench.bootstrap.projections import project_task, project_task_worker
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
DEMO_CONFIG = ROOT / "config/config.demo-local.toml"
LOCAL_CONFIG = ROOT / "config/config.local.toml"
DEFAULT_CONFIG = ROOT / "config/config.default.toml"
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


def test_the_console_profile_carries_both_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, DEMO_CONFIG)

    assert settings.optional_labs.mcp_adapter is True
    assert sorted(server.alias for server in settings.mcp.servers) == ["web", "word"]


def test_a_task_from_this_profile_can_reach_the_word_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The envelope is the assertion, not the server list.

    A Task carries the tool names the API froze at submission, and that freeze
    is the whole mechanism: `task_9bb8446a...` was submitted by a principal
    holding `mcp:word`, on a deployment whose config declared no Word server,
    and the scope bought nothing because the name was never in the envelope.
    """

    envelope = project_task(
        _load_profile(monkeypatch, DEMO_CONFIG)
    ).default_authorization_envelope

    assert "mcp_word_render_document" in envelope.allowed_tools
    assert "mcp_web_fetch_page" in envelope.allowed_tools
    assert "mcp_web_download_document" in envelope.allowed_tools


def test_the_writer_gets_word_and_the_researcher_gets_the_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audience, not just presence (ADR-027 §3.3).

    Both under one audience would be wrong in either direction: a writer able to
    read the outside world, or a renderer the writing node cannot see.
    """

    worker = project_task_worker(_load_profile(monkeypatch, DEMO_CONFIG))

    assert worker.mcp is not None
    audiences = {server.alias: server.audience for server in worker.mcp.servers}
    assert audiences == {"word": "synthesis", "web": "research"}


def test_the_console_profile_declines_the_export_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declined here, still shipped on by default.

    ADR-038 §2.1 lets a deployment choose; §4 says making `false` the repository
    default needs its own ADR. Both halves are asserted, because a change that
    moved the default would satisfy the first assertion silently.
    """

    demo = _load_profile(monkeypatch, DEMO_CONFIG)
    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    assert demo.workflow.export_requires_approval is False
    assert shipped.workflow.export_requires_approval is True


def test_the_console_profile_raises_both_budgets_a_document_run_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured ceilings, carried over from the two narrow profiles.

    Both were hit for real: `budget_exceeded: max_steps` on a render-then-revise
    loop, and `budget_exceeded: token_budget` before the node rendered anything.
    Neither failure names a budget in the console, so they read as the model
    giving up halfway.
    """

    demo = _load_profile(monkeypatch, DEMO_CONFIG)
    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    assert demo.runtime.max_steps > shipped.runtime.max_steps
    assert (
        demo.multi_agent.max_tokens_per_agent_invocation
        > shipped.multi_agent.max_tokens_per_agent_invocation
    )


def test_the_ordinary_local_profile_is_untouched_by_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union is a new file, not an edit to a narrow one.

    Anybody running the default local setup must be unaffected: the tool names
    are frozen into every new Task's envelope, and that envelope is re-applied
    on resume.
    """

    settings = _load_profile(monkeypatch, LOCAL_CONFIG)

    assert settings.optional_labs.mcp_adapter is False
    assert settings.mcp.servers == ()


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


def test_dev_script_probes_both_servers_before_either_is_assumed() -> None:
    result = _dev("demo-check")

    assert result.returncode == 0
    probed = [line.split() for line in result.stdout.splitlines() if line.strip()]
    assert [line[2] for line in probed] == ["word", "web"]
    assert "render_document" in probed[0]
    assert "fetch_page" in probed[1]
    assert "download_document" in probed[1]


def test_demo_worker_refuses_to_fake_the_task_path_without_a_provider() -> None:
    result = _dev("demo-worker", {"AW_SECRETS__DEEPSEEK_API_KEY": ""})

    assert result.returncode == 2
    assert "requires AW_SECRETS__DEEPSEEK_API_KEY" in result.stderr
    assert result.stdout.strip() == ""


def test_demo_worker_probes_both_servers_then_starts_the_real_graph(
    tmp_path: Path,
) -> None:
    """Which config file the arm exported, and in what order it checked.

    MCP discovery happens once at Worker startup and never hot-reloads, so a
    probe that ran after the Worker -- or only against one of the two servers --
    would let it come up missing the tool the profile exists for.
    """

    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-worker"],
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
        "config/config.demo-local.toml",
        "-m agent_workbench.apps.task_worker.main",
    ]
    assert "--label word --endpoint http://127.0.0.1:8765/mcp" in result.stderr
    assert "--label web --endpoint http://127.0.0.1:8767/mcp" in result.stderr


def test_demo_api_uses_the_same_profile(tmp_path: Path) -> None:
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-api"],
        cwd=ROOT,
        env={**os.environ, "PYTHON": str(probe)},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "config/config.demo-local.toml"


def test_the_usage_banner_lists_every_demo_command() -> None:
    """The banner is a `sed` range over the script's own header.

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

    for command in ("demo-check", "demo-api", "demo-worker"):
        assert command in documented
        assert command in banner.stdout
