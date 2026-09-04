"""The checked-in local profile for the computer-use MCP server (ADR-070).

The other profile tests in this directory pin what a profile *enables*. This
one mostly pins what it does not, and the asymmetry is the point: a resolved
tool name is normally frozen into the authorization envelope of every Task
submitted under that profile, and these eight can move the cursor, press keys
and choose which window receives both, on the machine the Worker is running
on. So the assertions below are that they
are *not* resolved -- neither into an envelope nor into a Worker binding.

That refusal was, until 2026-08-23, entirely incidental -- a consequence of
`retryable_effects = false` meeting two `continue` statements, asserted nowhere
that runs on this profile. Deleting either statement would have widened every
Task submitted under this profile by six screen-control tools and broken no
test. ADR-075 makes the refusal a decision; this file is what makes it a
regression when somebody undoes it.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from agent_workbench.bootstrap.projections import project_task, project_task_worker
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / "config/config.local.toml"
COMPUTER_CONFIG = ROOT / "config/config.computer-local.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:local-profile-test@127.0.0.1:5433/agent_workbench_local"
)

#: The eight the profile freezes, by name rather than by count. A server that
#: came up with seven of them is a different deployment wearing the same alias.
#:
#: Six until ADR-091 added the two that let a task reach a second application:
#: reading the approved list without opening a second dialog, and bringing an
#: approved window to the front.
SCREEN_TOOLS = (
    "request_access",
    "list_granted_applications",
    "activate_application",
    "screenshot",
    "left_click",
    "type",
    "key",
    "scroll",
)
LOCAL_NAMES = tuple(f"mcp_computer_{remote}" for remote in SCREEN_TOOLS)


def _load_profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    return load_settings(config_file=path)


def test_the_computer_profile_declares_the_server_it_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, COMPUTER_CONFIG)

    assert settings.optional_labs.mcp_adapter is True
    assert len(settings.mcp.servers) == 1
    server = settings.mcp.servers[0]
    assert server.alias == "computer"
    assert server.transport == "http"
    assert server.endpoint == "http://127.0.0.1:8768/mcp"
    assert server.tools == SCREEN_TOOLS
    # The declaration ADR-025 requires and ADR-075 keeps: a click is not a GET,
    # and a replayed one lands on whatever is under the cursor now.
    assert server.retryable_effects is False
    assert settings.model.main.tool_calling_required is False


def test_no_screen_tool_reaches_a_task_authorization_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing assertion of this file.

    Asserted against the base envelope as well, so that a change which widened
    the envelope by these names *and* widened the baseline could not satisfy
    it.
    """

    settings = _load_profile(monkeypatch, COMPUTER_CONFIG)
    baseline = _load_profile(monkeypatch, LOCAL_CONFIG)

    allowed = project_task(settings).default_authorization_envelope.allowed_tools
    for name in LOCAL_NAMES:
        assert name not in allowed
    base = project_task(baseline).default_authorization_envelope
    assert allowed == base.allowed_tools


def test_the_screen_tools_do_not_raise_the_deployments_risk_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second widening that would otherwise come for free with the first.

    Any non-empty MCP name list forces `max_tool_risk="external"` on the whole
    envelope. So a change admitting these would not only add eight tools; it
    would raise the ceiling for every Task submitted under this profile,
    including the ones that never touch a screen.
    """

    settings = _load_profile(monkeypatch, COMPUTER_CONFIG)
    envelope = project_task(settings).default_authorization_envelope

    assert envelope.max_tool_risk == "write"


def test_the_worker_still_learns_about_the_server_it_must_not_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The projection carries it through; the refusal happens later, once.

    Worth pinning in this direction: a "fix" that dropped the server from the
    Worker projection would also pass the envelope assertions above, and would
    take with it the log line that tells an operator why their screen tools are
    not there.
    """

    worker = project_task_worker(_load_profile(monkeypatch, COMPUTER_CONFIG))

    assert worker.mcp is not None
    assert [server.alias for server in worker.mcp.servers] == ["computer"]
    assert worker.mcp.servers[0].retryable_effects is False


def test_the_profile_points_at_the_port_the_server_actually_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_workbench.apps.computer_mcp.main import DEFAULT_PORT

    worker = project_task_worker(_load_profile(monkeypatch, COMPUTER_CONFIG))

    assert worker.mcp is not None
    assert worker.mcp.servers[0].endpoint == f"http://127.0.0.1:{DEFAULT_PORT}/mcp"


def test_the_ordinary_local_profile_has_never_heard_of_a_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, LOCAL_CONFIG)

    assert settings.mcp.servers == ()
    allowed = project_task(settings).default_authorization_envelope.allowed_tools
    for name in LOCAL_NAMES:
        assert name not in allowed


def test_the_console_profile_does_not_quietly_include_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`demo-local` is the union profile the console runs.

    It is the one a person reaches without choosing it -- somebody typing a
    request into Work is not selecting a profile -- which is exactly why the
    screen server must not be folded into it (ADR-070 §1).
    """

    console = _load_profile(monkeypatch, ROOT / "config/config.demo-local.toml")

    assert "computer" not in [server.alias for server in console.mcp.servers]


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


def test_dev_script_starts_the_server_from_the_signed_bundle() -> None:
    """Read rather than run, and the change is the reason.

    This used to execute the arm with `PYTHON=/bin/echo` and assert the module
    it printed. Since ADR-092 the arm builds and launches a signed `.app`, so
    running it here would put a bundle in somebody's ~/Applications every time
    the suite ran -- a test with a side effect on the machine it is testing.

    What is asserted is what that ADR actually requires: the server is not
    started from this shell. Launched that way it would have no bundle
    identity, no signature and no main-thread run loop, and
    `activate_application` would refuse every call.
    """

    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    arm = script.split("computer-server)", 1)[1].split("\n  ;;", 1)[0]

    assert "build_computer_app.sh" in arm
    assert "open -W -a" in arm
    # The direct invocation is what must NOT come back.
    assert "-m agent_workbench.apps.computer_mcp.main" not in arm


def test_the_bundle_build_is_idempotent_about_identity() -> None:
    """Rebuilding must not cost the person another trip to System Settings.

    TCC keys the Accessibility and Screen Recording grants on the bundle id
    together with the code signature, so a build that generated either afresh
    would silently revoke both -- and the revocation is invisible until an
    activation quietly stops working.
    """

    build = (ROOT / "scripts/build_computer_app.sh").read_text(encoding="utf-8")

    assert 'BUNDLE_ID="com.agent-workbench.computer-mcp"' in build
    assert "codesign --force --deep --sign -" in build
    # LSUIElement: registered with the window server, no Dock icon.
    assert "<key>LSUIElement</key><true/>" in build


def test_dev_script_probes_every_tool_by_name() -> None:
    """Counting them would call a seven-tool server healthy.

    The profile's `tools` list is the contract this server must satisfy, so a
    server that came up with seven of them is a different deployment wearing
    the same alias -- whether or not the names go anywhere afterwards.
    """

    result = _dev("computer-check")

    assert result.returncode == 0
    assert result.stdout.split() == [
        "scripts/smoke_mcp_server.py",
        "--label",
        "computer",
        "--endpoint",
        "http://127.0.0.1:8768/mcp",
        "--health-url",
        "http://127.0.0.1:8768/health",
        *[
            argument
            for remote in SCREEN_TOOLS
            for argument in ("--expect-tool", remote)
        ],
    ]


def test_there_is_no_computer_api_or_worker_arm_to_start() -> None:
    """Their absence is the decision, so it is asserted rather than assumed.

    A `computer-api` would freeze eight screen tools into every Task it submitted
    -- except that it would not, because the Task path refuses them at both
    ends, which makes the command a promise the platform does not keep. The
    honest shape is that the command does not exist (ADR-075).
    """

    for command in ("computer-api", "computer-worker"):
        result = _dev(command)
        assert result.returncode != 0

    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    assert "computer-api)" not in script
    assert "computer-worker)" not in script


def test_the_usage_banner_documents_both_computer_commands() -> None:
    """The banner is a `sed` range over the script's own header.

    Adding an arm without extending the range leaves it undocumented, and a
    line-number range is exactly the thing nobody notices by hand.
    """

    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    documented = {
        line.split()[2]
        for line in script.splitlines()
        if line.startswith("#   scripts/dev.sh ")
    }
    banner = _dev("")

    for command in ("computer-server", "computer-check"):
        assert command in documented
        assert command in banner.stdout


# --- Windows (ADR-0108) ----------------------------------------------------------

WINDOWS_COMPUTER_LAUNCHER = ROOT / "scripts" / "computer.cmd"


def test_windows_gets_its_own_launcher_for_the_screen_server() -> None:
    """The one part of the Windows route that cannot be a container.

    A screen adapter needs the desktop, and the desktop is on the host; so the
    server runs beside the containers, started by this file, and asks the
    machine for uv and nothing else. Held to the two rules every `.cmd` here
    is held to (`tests/deployment/test_compose.py` says why): ASCII, CRLF,
    and no `rem` line that cmd.exe would execute.
    """

    assert WINDOWS_COMPUTER_LAUNCHER.is_file()
    raw = WINDOWS_COMPUTER_LAUNCHER.read_bytes()
    raw.decode("ascii")
    assert b"\r\n" in raw
    assert re.search(rb"[^\r]\n", raw) is None, "every line ending must be CRLF"
    lines = raw.decode("ascii").splitlines()
    talkative = [
        line
        for line in lines
        if line.lstrip().lower().startswith("rem") and any(ch in line for ch in "&|<>")
    ]
    assert not talkative, f"cmd.exe executes these rem lines: {talkative}"

    executable = [
        line
        for line in lines
        if line.strip() and not line.lstrip().lower().startswith("rem")
    ]
    joined = "\n".join(executable)
    assert "uv sync --frozen --extra computer-use" in joined
    assert "agent-computer-mcp" in joined
    # The port the API's tunnel dials (`docker/run-api-local.sh`).
    assert "8768" in " ".join(
        line for line in executable if line.lower().startswith("echo")
    )


def test_the_windows_half_of_the_extra_is_pillow_and_only_pillow() -> None:
    """No pywin32, no pyautogui: `win32.py` reaches the platform through
    ctypes. What it cannot do without is an image library, and the one it
    uses already arrives transitively -- declared so "we use this" is a claim
    the lock file backs."""

    with (ROOT / "pyproject.toml").open("rb") as handle:
        extra = tomllib.load(handle)["project"]["optional-dependencies"]["computer-use"]
    windows = [spec for spec in extra if "win32" in spec]
    darwin = [spec for spec in extra if "darwin" in spec]
    assert len(windows) == 1 and windows[0].startswith("pillow")
    assert len(darwin) == 3
    assert all("sys_platform" in spec for spec in extra), (
        "every entry is platform-marked"
    )
