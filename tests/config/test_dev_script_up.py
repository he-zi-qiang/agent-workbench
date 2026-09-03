"""``scripts/dev.sh up``: one command, and the order it will not let you get wrong.

The console needs six processes in an order that is not guessable. Two of the
orderings are load-bearing: ``demo-api`` probes the sandbox MCP server before
it will start, and a Worker freezes its MCP catalogue once at startup, so a
server that comes up late leaves a Worker that is healthy and missing the tool
the profile exists for.

That order used to live only in ``docs/running-locally.md`` -- and the doc had
it wrong: its console list named ``word-server`` and ``web-server`` and never
``sandbox-server``, so following it exactly made ``demo-api`` fail on a probe
the doc did not mention. These tests pin the order in the place that now owns
it, and they pin it through ``--plan``, which computes the whole sequence and
starts nothing: no database, no provider key, no model download.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from agent_workbench.bootstrap.provider_key import ENV_VAR, KEY_FILE_ENV_VAR

ROOT = Path(__file__).resolve().parents[2]
DEV_SCRIPT = ROOT / "scripts" / "dev.sh"
#: Everything that reads an MCP catalogue must start after every server.
MCP_SERVERS = ("word-server", "web-server", "sandbox-server")
CATALOGUE_READERS = ("demo-api", "demo-worker")


def _run(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """One arm, with no key and no key file from the machine running this.

    ``AW_KEY_FILE=""`` is what says "this deployment has no key file": on the
    one machine that does have a key sitting in the default place -- the
    developer's -- the keyless assertions below would otherwise quietly stop
    testing the keyless path.
    """

    environment_ = {**os.environ, KEY_FILE_ENV_VAR: ""}
    environment_.pop(ENV_VAR, None)
    environment_.update(environment or {})
    return subprocess.run(
        ["bash", str(DEV_SCRIPT), *arguments],
        cwd=ROOT,
        env=environment_,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _plan(with_key: bool) -> list[str]:
    result = _run(
        "up",
        "--plan",
        environment={ENV_VAR: "contract-only-not-a-real-key"} if with_key else {},
    )
    assert result.returncode == 0, result.stderr
    steps = next(
        line for line in result.stdout.splitlines() if line.startswith("steps:")
    )
    return steps.split(":", 1)[1].split()


def test_the_console_plan_starts_the_sandbox_server_the_doc_forgot() -> None:
    """The one that made the written instructions unfollowable.

    `demo-api` probes `http://127.0.0.1:8766/mcp` and expects `run_python`,
    under `set -euo pipefail`. Measured 2026-09-02 against a dead port: the
    probe gives up after 11 seconds and exits 1, so the arm dies there. Any
    sequence that starts `demo-api` without this server does not work, however
    carefully it was written down.
    """

    assert "sandbox-server" in _plan(with_key=True)


@pytest.mark.parametrize("reader", CATALOGUE_READERS)
@pytest.mark.parametrize("server", MCP_SERVERS)
def test_every_mcp_server_precedes_everything_that_reads_a_catalogue(
    server: str, reader: str
) -> None:
    plan = _plan(with_key=True)
    assert plan.index(server) < plan.index(reader), f"{server} must precede {reader}"


def test_the_keyless_plan_asks_for_no_mcp_server_and_no_console_profile() -> None:
    """A keyless start is a smaller deployment, not a broken one.

    `demo-api` refuses without a key on purpose (a console without Chat looks
    identical to a working one from the browser), so `up` picks the arms that
    do start: plain `api`, and a Worker on the demo graph.
    """

    plan = _plan(with_key=False)

    assert plan == ["services", "migrate", "api", "ingest", "worker"]
    for server in MCP_SERVERS:
        assert server not in plan


@pytest.mark.parametrize("with_key", [True, False], ids=["console", "keyless"])
def test_both_plans_start_the_ingestion_worker(with_key: bool) -> None:
    """The one absence a browser cannot see.

    Without it an upload sits in `processing` for ever, and the page renders
    that identically to "vectorizing" -- `knowledge_bases.py` has no notion of
    a stale one. It is also what creates the Qdrant collection and binds the
    read alias, so a stack without it has no index at all.
    """

    assert "ingest" in _plan(with_key=with_key)


@pytest.mark.parametrize("with_key", [True, False], ids=["console", "keyless"])
def test_a_plan_starts_nothing(with_key: bool) -> None:
    """`--plan` has to be free of side effects, or it is not an answer to
    "what will this do" -- it is the doing."""

    before = (
        {p.name for p in (ROOT / "var" / "run").glob("*.pid")}
        if (ROOT / "var" / "run").is_dir()
        else set()
    )

    _plan(with_key=with_key)

    after = (
        {p.name for p in (ROOT / "var" / "run").glob("*.pid")}
        if (ROOT / "var" / "run").is_dir()
        else set()
    )
    assert before == after


def test_up_refuses_a_port_somebody_else_is_already_serving() -> None:
    """The realistic collision is this project's own Compose stack.

    Both publish 8000 on loopback. Without this check the failure is an API
    that exits on "address already in use" a minute into loading BGE-M3, which
    reads as a broken checkout far more often than as two stacks wanting one
    port.
    """

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = _run("up", environment={"AW_API_PORT": str(port)})

    assert result.returncode == 2
    assert f"already serving 127.0.0.1:{port}" in result.stderr
    assert "docker compose" in result.stderr, "it must name the likely culprit"


def test_down_and_status_are_safe_with_nothing_running() -> None:
    """Both are what a confused person reaches for first."""

    status = _run("status")
    assert status.returncode == 0
    assert "NAME" in status.stdout

    down = _run("down")
    assert down.returncode == 0
    assert "nothing that `up` started is running" in down.stderr


def test_logs_without_a_name_lists_only_what_up_manages() -> None:
    """`var/log/` also collects whatever anybody redirected into it by hand.

    Offering those as choices makes the list a worse answer than no list: this
    checkout has ~40 such files, from `api-repro3` to `vitest-flake`.
    """

    result = _run("logs")

    assert result.returncode == 2
    offered = {
        line.strip() for line in result.stderr.splitlines() if line.startswith("  ")
    }
    assert not offered - {
        "word-server",
        "web-server",
        "sandbox-server",
        "api",
        "demo-api",
        "ingest",
        "worker",
        "demo-worker",
    }


def test_the_banner_leads_with_the_one_command() -> None:
    """`up` is what a person wants; the twenty arms are what it is made of.

    The banner is a `sed` range over the script's own header, and the range is
    exactly the thing nobody remembers to widen -- so this asserts the four new
    commands actually render, not merely that they were typed into the header.
    """

    banner = _run().stdout

    for command in ("up", "down", "status", "logs"):
        assert f"scripts/dev.sh {command}" in banner, command


def _up_arm() -> str:
    """The text of the `up)` case arm."""

    script = DEV_SCRIPT.read_text(encoding="utf-8")
    start = script.index("\nup)\n")
    return script[start : script.index("\n  ;;\n", start)]


def _step_total(with_key: bool) -> int:
    result = _run(
        "up",
        "--plan",
        environment={ENV_VAR: "contract-only-not-a-real-key"} if with_key else {},
    )
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("shown as:")
    )
    return int(line.split(":", 1)[1].strip().split()[0])


def test_the_step_count_matches_the_steps_each_path_actually_runs() -> None:
    """`[7/6]` is what the first version printed on the keyless path.

    The total was hand-counted in one place and the `_step_begin` calls lived
    in another, so the two drifted the moment a branch gained a step. This
    counts the calls the arm reaches on each path, from the script's own text,
    and holds the number `--plan` advertises to it.

    Counting by branch rather than in total: three of the calls are inside the
    console-only block and one is inside its `else`, so neither path runs all
    of them.
    """

    arm = _up_arm()
    console_block = arm[arm.index('if [ "$PLAN_PROFILE" = "console" ]; then') :]
    console_only, keyless_only = console_block.split("\n  else\n", 1)
    keyless_only = keyless_only.split("\n  fi\n", 1)[0]

    every = arm.count("_step_begin ")
    console_steps = every - keyless_only.count("_step_begin ")
    keyless_steps = every - console_only.count("_step_begin ")

    assert console_steps == _step_total(with_key=True)
    assert keyless_steps == _step_total(with_key=False)
    # And the arm really does branch, or the two counts above are the same
    # number arrived at twice.
    assert console_steps != keyless_steps


def test_a_recycled_pid_is_not_something_down_will_kill(tmp_path: Path) -> None:
    """The comment used to claim this protection while the code lacked it.

    A pid file outlives the process it names -- a reboot, or a `kill -9` that
    skipped the cleanup -- and pids are recycled. Without an ownership check
    `down` sends TERM to whatever unrelated program inherited the number.

    The stand-in here is this very pytest process: a live pid that is certainly
    not one of ours. `down` must delete the stale file and report nothing
    stopped, rather than signal it.

    Its control case is unusually direct. Removing `_is_ours` from `_running`
    and rerunning this test does not produce a red assertion -- it produces
    `exit=143`, because the code under test TERMs the pid it was handed, and
    that pid is the test runner. Measured 2026-09-02.
    """

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stale = run_dir / "demo-api.pid"
    stale.write_text(str(os.getpid()), encoding="utf-8")

    result = _run("down", environment={"AW_RUN_DIR": str(run_dir)})

    assert result.returncode == 0
    assert "demo-api stopped" not in result.stderr, "it signalled a stranger"
    assert "nothing that `up` started is running" in result.stderr
    assert not stale.exists(), "the stale file should be cleared"


def test_status_does_not_call_a_recycled_pid_running(tmp_path: Path) -> None:
    """Same rule, read from the other side.

    `status` is what a confused person reads first; a stale pid reported as
    `running` sends them looking for a process that is not there.
    """

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ingest.pid").write_text(str(os.getpid()), encoding="utf-8")

    result = _run("status", environment={"AW_RUN_DIR": str(run_dir)})

    assert result.returncode == 0
    row = next(line for line in result.stdout.splitlines() if line.startswith("ingest"))
    assert "gone" in row, row


def _plan_text(*flags: str, with_key: bool) -> str:
    result = _run(
        "up",
        "--plan",
        *flags,
        environment={ENV_VAR: "contract-only-not-a-real-key"} if with_key else {},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_plan_says_whether_this_start_will_have_retrieval() -> None:
    """The absence this stack cannot show you afterwards.

    A process with no `embedding` extra serves every route, answers
    `/health/ready` 200, and comes up in five seconds instead of two minutes --
    measured 2026-08-30. From a browser it is a fast, healthy console that
    happens to retrieve nothing: an upload never becomes searchable, and the
    only complaint is in the ingestion worker's log, which exits on its first
    line.

    `up` cannot fix that for you -- the extra is gigabytes -- but it must not
    stay quiet about it, which is ADR-102's rule applied to a launcher.
    """

    line = next(
        line
        for line in _plan_text(with_key=True).splitlines()
        if line.startswith("retrieval:")
    )

    assert "real BGE-M3" in line or "ABSENT" in line, line
    if "ABSENT" in line:
        assert "--with-retrieval" in line, "it must name the way out"


def test_with_retrieval_is_an_accepted_flag_and_an_unknown_one_is_not() -> None:
    """`--plan` with the flag must still start nothing, so this is safe to run.

    The negative half matters as much: a typo that is silently ignored would
    leave somebody believing they asked for the extra when they did not, and
    they would find out four minutes later from an empty search.
    """

    assert _plan_text("--with-retrieval", with_key=True).startswith("profile:")

    bad = _run("up", "--plan", "--with-retreival")
    assert bad.returncode == 2
    assert "unknown option" in bad.stderr
    assert "--with-retrieval" in bad.stderr, "the refusal should spell it right"


def test_the_readiness_wait_needs_no_curl() -> None:
    """`curl` is not guaranteed on a minimal WSL or container image.

    While the wait used it, its absence was indistinguishable from a broken
    API: the loop never succeeded, spun for the full 300-second deadline, and
    then reported that the API had not answered -- naming the wrong thing for
    five minutes. It now goes through `docker/wait_for_http.py`, which is both
    the helper the container topology already uses for Qdrant and a file that
    imports nothing outside the standard library.
    """

    executable = [
        line
        for line in DEV_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    calls_curl = [line for line in executable if "curl" in line]

    # The one that may remain is the sentence telling somebody how to install
    # uv, which is advice rather than something this script runs.
    assert all("astral.sh/uv/install.sh" in line for line in calls_curl), calls_curl
    assert any("docker/wait_for_http.py" in line for line in executable)

    waiter = ROOT / "docker" / "wait_for_http.py"
    source = waiter.read_text(encoding="utf-8")
    for third_party in ("import httpx", "import requests", "import aiohttp"):
        assert third_party not in source, third_party


def test_up_separates_docker_absent_from_docker_not_running() -> None:
    """Two failures that look alike and need different answers.

    `scripts\\stack.cmd` has separated them since it was written, because only
    the first is obvious from what Docker prints. Measured 2026-09-02 with the
    engine stopped, before this: `up` reached the services step and emitted
    Docker's own "Cannot connect to the Docker daemon" with nothing about which
    step that was or what to do about it.

    The WSL sentence is load-bearing on its own. There, `docker` can be absent
    from the shell while Docker Desktop runs perfectly well on the Windows side,
    and no amount of restarting it helps -- the fix is a checkbox in Settings >
    Resources > WSL Integration, which nothing in Docker's own error mentions.
    """

    script = DEV_SCRIPT.read_text(encoding="utf-8")
    body = script[script.index("_require_docker()") : script.index("_plan()")]

    assert "command -v docker" in body, "the absent case must be probed by running it"
    assert "docker info" in body, "a shim on PATH resolves and then fails"
    assert "WSL Integration" in body
    assert "not running" in body

    # And it must be reached before anything is started, or the answer arrives
    # after a container was already created.
    arm = _up_arm()
    assert arm.index("_require_docker") < arm.index('"$0" services')
