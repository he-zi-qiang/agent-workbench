"""Isolation, verified against a real container (ADR-029 §4).

The ADR is explicit that this cannot be asserted by checking that we set a
flag: "验收里必须包含尝试联网并断言失败、尝试写只读根并断言失败、超时被杀掉". A test
that reads ``ISOLATION_FLAGS`` and finds ``--network=none`` in it proves that
somebody typed the string, and would keep passing after a rename, a runtime
that ignores the flag, or an image with its own network namespace.

So each test here runs a script that *tries* the thing, and each is paired with
a control that does the permitted version of it -- otherwise a sandbox that
failed every script would look like a sandbox that isolates.

Skipped, loudly, when there is no container runtime or the interpreter image is
not present locally: pulling one inside a test would make an ordinary suite run
depend on the network.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess

import pytest

from agent_workbench.apps.sandbox_mcp.contract import parse_run_request
from agent_workbench.apps.sandbox_mcp.executor import (
    DEFAULT_CONTAINER_RUNTIME,
    DEFAULT_SANDBOX_IMAGE,
    MAX_OUTPUT_FILES,
    MAX_STDOUT_BYTES,
    WALL_CLOCK_SECONDS,
    SandboxExecutionError,
    SandboxExecutor,
    SandboxOutcome,
)


def _require_runtime() -> None:
    if shutil.which(DEFAULT_CONTAINER_RUNTIME) is None:
        pytest.skip(f"{DEFAULT_CONTAINER_RUNTIME} is not installed")
    probe = subprocess.run(
        [DEFAULT_CONTAINER_RUNTIME, "image", "inspect", DEFAULT_SANDBOX_IMAGE],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"{DEFAULT_SANDBOX_IMAGE} is not present locally; "
            f"run `{DEFAULT_CONTAINER_RUNTIME} pull {DEFAULT_SANDBOX_IMAGE}`"
        )


def _run(
    script: str,
    *,
    wall_clock_seconds: int = WALL_CLOCK_SECONDS,
) -> SandboxOutcome:
    _require_runtime()
    executor = SandboxExecutor(wall_clock_seconds=wall_clock_seconds)
    return asyncio.run(executor.run(parse_run_request({"script": script})))


def test_a_pure_computation_succeeds() -> None:
    """The control group for everything below.

    Every other test here asserts a refusal, and a sandbox that refused to run
    anything at all would satisfy all of them.
    """

    outcome = _run("print(sum(range(101)))")

    assert outcome.exit_code == 0
    assert outcome.stdout.strip() == "5050"
    assert outcome.stderr == ""


def test_a_script_cannot_reach_the_network() -> None:
    """ADR-029 §3.2. This is the premise the rest of the ADR stands on."""

    outcome = _run(
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "print('CONNECTED')\n"
    )

    assert outcome.exit_code != 0
    assert "CONNECTED" not in outcome.stdout
    assert "Network is unreachable" in outcome.stderr


def test_a_script_cannot_resolve_a_name_either() -> None:
    """No resolver, not just no route: DNS is a network egress of its own."""

    outcome = _run(
        "import socket\nsocket.gethostbyname('example.com')\nprint('RESOLVED')\n"
    )

    assert outcome.exit_code != 0
    assert "RESOLVED" not in outcome.stdout


def test_a_script_cannot_write_the_root_filesystem_but_can_write_its_own_layer() -> (
    None
):
    """``/tmp`` rather than ``/etc`` on purpose.

    ``/etc`` is refused by its ownership even on a writable root, so a test
    aimed there would keep passing with ``--read-only`` removed. ``/tmp`` is
    world-writable in the image: the only thing that stops a write to it is the
    read-only root.
    """

    refused = _run("open('/tmp/probe', 'w').write('x')")
    assert refused.exit_code != 0
    assert "Read-only file system" in refused.stderr

    permitted = _run(
        "import pathlib\n"
        "pathlib.Path('out.txt').write_text('x')\n"
        "print(pathlib.Path('out.txt').read_text())\n"
    )
    assert permitted.exit_code == 0
    assert permitted.stdout.strip() == "x"


def test_the_script_does_not_run_as_root() -> None:
    outcome = _run("import os; print(os.getuid(), os.geteuid())")

    assert outcome.stdout.strip() == "65534 65534"


def test_no_host_path_is_visible_inside_the_container() -> None:
    """No mounts, so this repository is not reachable from the script."""

    outcome = _run(
        "import os\n"
        "print(sorted(os.listdir('/')))\n"
        "print(os.path.exists('/Users'), os.path.exists('/host'))\n"
    )

    assert outcome.exit_code == 0
    assert "False False" in outcome.stdout
    assert "agent_workbench" not in outcome.stdout


def test_an_endless_script_is_killed_and_a_quick_one_is_not() -> None:
    """The wall clock is a kill, not a note in the result."""

    with pytest.raises(SandboxExecutionError) as refused:
        _run("while True:\n    pass\n", wall_clock_seconds=3)
    assert refused.value.code == "timeout"

    quick = _run("print('done')", wall_clock_seconds=3)
    assert quick.stdout.strip() == "done"


def test_nothing_survives_between_two_calls() -> None:
    """One container per call, and it is destroyed with the call.

    Written outside the working directory on purpose: a file in the working
    directory would come back as an output, which proves the collector works
    rather than that the container was fresh.
    """

    first = _run("open('/sandbox/leftover', 'w').write('x')")
    assert first.exit_code == 0

    second = _run("import os; print(os.path.exists('/sandbox/leftover'))")
    assert second.stdout.strip() == "False"


def test_the_process_ceiling_holds() -> None:
    """A script cannot fork its way past the resource limits."""

    outcome = _run(
        "import subprocess, sys\n"
        "children = []\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        children.append(subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']))\n"
        "except Exception as error:\n"
        "    print('STOPPED', type(error).__name__)\n"
        "else:\n"
        "    print('UNBOUNDED', len(children))\n",
        wall_clock_seconds=20,
    )

    assert "UNBOUNDED" not in outcome.stdout


def test_oversized_stdout_is_a_structured_error_and_the_limit_itself_is_not() -> None:
    with pytest.raises(SandboxExecutionError) as refused:
        _run(f"import sys; sys.stdout.write('x' * {MAX_STDOUT_BYTES + 1})")
    assert refused.value.code == "stdout_too_large"

    at_limit = _run(f"import sys; sys.stdout.write('x' * {MAX_STDOUT_BYTES})")
    assert len(at_limit.stdout) == MAX_STDOUT_BYTES


def test_too_many_outputs_is_a_structured_error_and_the_limit_itself_is_not() -> None:
    with pytest.raises(SandboxExecutionError) as refused:
        _run(
            "import pathlib\n"
            f"for index in range({MAX_OUTPUT_FILES + 1}):\n"
            "    pathlib.Path(f'f{index}.txt').write_text('x')\n"
        )
    assert refused.value.code == "too_many_outputs"

    at_limit = _run(
        "import pathlib\n"
        f"for index in range({MAX_OUTPUT_FILES}):\n"
        "    pathlib.Path(f'f{index}.txt').write_text('x')\n"
    )
    assert len(at_limit.outputs) == MAX_OUTPUT_FILES


def test_files_go_in_and_files_come_back() -> None:
    """The shape ADR-029 §3.1 names, end to end through a real container."""

    _require_runtime()
    csv_bytes = b"region,amount\nnorth,100\nsouth,250\neast,25\nwest,7\n"
    request = parse_run_request(
        {
            "script": (
                "import csv, pathlib\n"
                "rows = list(csv.DictReader(open('sales.csv')))\n"
                "total = sum(int(row['amount']) for row in rows)\n"
                "print('total', total)\n"
                "pathlib.Path('summary.txt').write_text(f'total={total}\\n')\n"
            ),
            "inputs": [
                {
                    "name": "sales.csv",
                    "content_base64": base64.b64encode(csv_bytes).decode("ascii"),
                }
            ],
        }
    )

    outcome = asyncio.run(SandboxExecutor().run(request))

    assert outcome.exit_code == 0
    assert outcome.stdout.strip() == "total 382"
    assert {file.name: file.content for file in outcome.outputs} == {
        "summary.txt": b"total=382\n"
    }


def test_the_probe_finds_the_runtime_and_reports_a_missing_one() -> None:
    _require_runtime()

    assert asyncio.run(SandboxExecutor().probe()) is True
    assert (
        asyncio.run(SandboxExecutor(runtime="no-such-container-runtime").probe())
        is False
    )
