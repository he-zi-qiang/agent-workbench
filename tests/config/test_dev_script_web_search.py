"""``scripts/dev.sh`` decides Chat's web search the way the container does.

Until ADR-104 the ``demo-api`` and ``demo-worker`` arms ran a bare
``export AW_RESEARCH__ENABLED=true``. That overrode two people: an operator
who had exported ``false`` in the shell, and -- since ADR-103 -- anyone who
had switched web search off on the System page. A stored switch ranks below
the environment, so the page reported every native start as ``overridden``
and blamed an environment nobody had set. ``docker/run-api-local.sh`` had
been taught to step aside in that same batch; the native arms now run the
same probe (``docker/decide_web_search.py``) and export only when it says
yes.

Three shapes of test, because the halves fail differently. A stand-in probe
pins what each arm does with each answer and with an operator's own value. A
text rule pins that the guard is the container launcher's guard, in both
arms. One run through the *real* probe pins that a file the store wrote is
what the shell reads -- the whole feature, with nothing faked on the path
from the page to the process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_workbench.application.switches import SwitchStore
from agent_workbench.bootstrap.provider_key import ENV_VAR, KEY_FILE_ENV_VAR

ROOT = Path(__file__).resolve().parents[2]
DEV_SCRIPT = ROOT / "scripts" / "dev.sh"
API_LAUNCHER = ROOT / "docker" / "run-api-local.sh"
PROBE = "docker/decide_web_search.py"
ARMS = ("demo-api", "demo-worker")
#: What the process reports when the arm exported nothing: the settings
#: loader then decides, from the stored switch and the TOML files.
UNDECIDED = "unset"


def _stand_in(tmp_path: Path, on_probe: str) -> Path:
    """A ``$PYTHON`` that runs ``on_probe`` for the probe and reports the exec.

    Three kinds of invocation reach ``$PYTHON`` from either arm: the
    web-search probe, the smoke probes for the MCP servers, and the ``-m``
    exec of the process itself. The stand-in runs ``on_probe`` (a shell
    snippet) for the first, prints the variable the process would inherit for
    the last, and stays silent for the smoke probes. The script sends the
    probe's output to stderr, which is why stdout below is exactly one line.
    """

    script = tmp_path / "python"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  {PROBE}) {on_probe} ;;\n"
        '  -m) printf "%s\\n" "${AW_RESEARCH__ENABLED-' + UNDECIDED + '}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _run(
    arm: str, python: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHON": str(python),
        # A key from somewhere, or both arms refuse before deciding anything.
        ENV_VAR: "contract-only-not-a-real-key",
        # And no key *file* unless the test names one: the developer's own
        # switches file must not leak into an assertion about the script.
        KEY_FILE_ENV_VAR: "",
    }
    # Nor may the developer's shell decide for the test.
    env.pop("AW_RESEARCH__ENABLED", None)
    env.update(environment)
    return subprocess.run(
        ["bash", str(DEV_SCRIPT), arm],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize(
    ("answer", "inherited"),
    [("exit 0", "true"), ("exit 1", UNDECIDED)],
    ids=["probe-says-yes", "probe-says-no"],
)
def test_the_arm_exports_only_what_the_probe_allows(
    tmp_path: Path, arm: str, answer: str, inherited: str
) -> None:
    result = _run(arm, _stand_in(tmp_path, f"echo probe-ran; {answer}"), {})

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [inherited]
    assert "probe-ran" in result.stderr


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("value", ["false", "true"])
def test_an_operators_own_value_is_left_alone(
    tmp_path: Path, arm: str, value: str
) -> None:
    """The case the old line got wrong before there were any switches.

    `export AW_RESEARCH__ENABLED=true` ran after the shell's own export, so a
    developer who had said `false` got web search anyway -- and, from the
    command line, no way to tell which value the process had used. The
    container launcher's rule is that an explicit value is not even put to
    the probe; so is this one.
    """

    result = _run(
        arm,
        _stand_in(tmp_path, "echo probe-ran; exit 0"),
        {"AW_RESEARCH__ENABLED": value},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [value]
    assert "probe-ran" not in result.stderr, "an explicit value was put to the probe"


@pytest.mark.parametrize("arm", ARMS)
def test_an_empty_value_is_nobody_deciding(tmp_path: Path, arm: str) -> None:
    """Empty is what Compose hands the container for an unset host variable.

    The two launchers have to read it the same way -- `:-` rather than `-` in
    the guard -- or a stack and a shell would disagree about whether an empty
    string is a decision.
    """

    result = _run(
        arm,
        _stand_in(tmp_path, "echo probe-ran; exit 0"),
        {"AW_RESEARCH__ENABLED": ""},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["true"]
    assert "probe-ran" in result.stderr


def _commands(text: str) -> str:
    """The script's executable lines only, comments removed.

    Its comments quote the very line they exist to replace -- the bare
    `export AW_RESEARCH__ENABLED=true` -- so a count over the whole file finds
    the explanation and reports the opposite of the truth. The same rule
    `test_compose.py` applies to the Windows launcher, for the same reason.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _arm(script: str, name: str) -> str:
    """The text of one `case` arm, from its label to its `;;`."""

    start = script.index(f"\n{name})\n")
    return script[start : script.index("\n  ;;\n", start)]


def test_both_arms_guard_the_export_the_way_the_container_launcher_does() -> None:
    """The rule as text, in the shape `test_compose.py` holds the container to.

    The stand-in above covers what the script does with an answer; this pins
    that what sits in front of the export is the *same* guard as
    `docker/run-api-local.sh` -- unset or empty is decided, anything else is
    left alone -- in both arms, and that no unguarded export survived the
    edit anywhere in the file.
    """

    script = _commands(DEV_SCRIPT.read_text(encoding="utf-8"))
    guard = 'if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then'
    assert guard in API_LAUNCHER.read_text(encoding="utf-8")
    assert script.count("export AW_RESEARCH__ENABLED=true") == len(ARMS)
    for name in ARMS:
        arm = _arm(script, name)
        assert guard in arm, name
        enabling = arm.index("export AW_RESEARCH__ENABLED=true")
        assert arm.index(guard) < arm.index(PROBE) < enabling, name
        # A stand-in `$PYTHON` echoes to stdout, and stdout is the process's.
        assert f'"$PYTHON" {PROBE} >&2' in arm, name


def test_the_two_launchers_share_one_probe() -> None:
    """One file answers for both ways of starting the console.

    Two probes would be two rules the day one of them is edited, and the
    System page would then describe a native start and a container start of
    the same stored choice differently. ADR-103 §5.5 declined to touch the
    script for exactly that fear; sharing the probe is what answers it.
    """

    assert (ROOT / PROBE).is_file()
    assert PROBE in API_LAUNCHER.read_text(encoding="utf-8")
    assert PROBE in DEV_SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("stored", "inherited", "said"),
    [
        (None, "true", "chat web_search is on for this start"),
        (False, UNDECIDED, "research.enabled is stored as false"),
        (True, UNDECIDED, "research.enabled is stored as true"),
    ],
    ids=["nothing-stored", "stored-off", "stored-on"],
)
def test_a_choice_the_store_wrote_is_what_the_shell_reads(
    tmp_path: Path, stored: bool | None, inherited: str, said: str
) -> None:
    """End to end, with nothing faked between the page and the process.

    The store writes the file the way the API does; the script resolves the
    key directory the way it always has; the real probe, run by this
    interpreter under the `PYTHONPATH=src` the script exports, reads it. A
    stored value either way leaves the variable unset -- the loader applies
    "off" outright, and applies "on" or holds it without a key -- and only
    "nothing stored" turns into an export.
    """

    key_dir = tmp_path / "outside-the-checkout"
    key_dir.mkdir()
    if stored is not None:
        SwitchStore(path=key_dir / "switches.json", checkout_root=None).set(
            "research.enabled", stored
        )
    delegate = _stand_in(tmp_path, f'exec "{sys.executable}" "$@"')

    result = _run("demo-api", delegate, {KEY_FILE_ENV_VAR: str(key_dir / "key")})

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [inherited]
    assert said in result.stderr
