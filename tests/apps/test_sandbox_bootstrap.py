"""What the in-container program collects, and what it refuses to collect.

These run the bootstrap on a temporary directory in this process. That is the
half of it that is about ceilings and file collection, and it is cheap enough
to run on every commit. The half that is about isolation is not testable this
way and is not tested this way -- ``test_sandbox_isolation.py`` runs a real
container for that, because a test that asserts we passed ``--network=none``
proves only that we wrote the flag down.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent_workbench.apps.sandbox_mcp._bootstrap import (
    MAX_PROGRESS_RECORD_BYTES,
    MAX_PROGRESS_TOTAL_BYTES,
    PROGRESS_PREFIX,
    execute,
)

LIMITS: dict[str, Any] = {
    "wall_clock_seconds": 20,
    "max_stdout_bytes": 1024,
    "max_stderr_bytes": 1024,
    "max_output_files": 3,
    "max_output_file_bytes": 512,
    "max_total_output_bytes": 1024,
}


def _run(
    tmp_path: Path,
    script: str,
    inputs: list[tuple[str, bytes]] | None = None,
    **limit_overrides: int,
) -> dict[str, Any]:
    return execute(
        {
            "script": script,
            "inputs": [
                {
                    "name": name,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
                for name, content in (inputs or [])
            ],
            "limits": {**LIMITS, **limit_overrides},
        },
        root=str(tmp_path),
    )


def _outputs(envelope: dict[str, Any]) -> dict[str, bytes]:
    return {
        item["name"]: base64.b64decode(item["content_base64"])
        for item in envelope["outputs"]
    }


def test_a_script_reads_its_inputs_and_its_new_files_come_back(tmp_path: Path) -> None:
    envelope = _run(
        tmp_path,
        "import pathlib\n"
        "text = pathlib.Path('in.txt').read_text()\n"
        "print('read', text)\n"
        "pathlib.Path('out.txt').write_text(text.upper())\n",
        [("in.txt", b"abc")],
    )

    assert envelope["exit_code"] == 0
    assert envelope["stdout"] == "read abc\n"
    assert _outputs(envelope) == {"out.txt": b"ABC"}


def test_an_untouched_input_is_not_returned_but_an_edited_one_is(
    tmp_path: Path,
) -> None:
    """The control pair for "outputs are what changed".

    Without the second half, an implementation that returned nothing at all
    would pass the first half.
    """

    untouched = _run(tmp_path / "a", "print('nothing')", [("in.txt", b"abc")])
    assert _outputs(untouched) == {}

    edited = _run(
        tmp_path / "b",
        "import pathlib; pathlib.Path('in.txt').write_text('xyz')",
        [("in.txt", b"abc")],
    )
    assert _outputs(edited) == {"in.txt": b"xyz"}


def test_an_input_rewritten_with_the_same_bytes_is_not_an_output(
    tmp_path: Path,
) -> None:
    """Same length, same content, new mtime. Identity is the bytes."""

    envelope = _run(
        tmp_path,
        "import pathlib; pathlib.Path('in.txt').write_text('abc')",
        [("in.txt", b"abc")],
    )

    assert _outputs(envelope) == {}


def test_a_failing_script_is_a_result_and_not_a_sandbox_error(
    tmp_path: Path,
) -> None:
    envelope = _run(tmp_path, "raise SystemExit(3)")

    assert "error" not in envelope
    assert envelope["exit_code"] == 3


def test_a_traceback_reaches_stderr(tmp_path: Path) -> None:
    envelope = _run(tmp_path, "raise ValueError('boom')")

    assert envelope["exit_code"] == 1
    assert "ValueError: boom" in envelope["stderr"]
    assert envelope["stdout"] == ""


def test_the_script_cannot_forge_the_envelope(tmp_path: Path) -> None:
    """Its stdout is captured, not passed through.

    A script that prints a well-formed envelope must show up as a script that
    printed something, not as a result.
    """

    envelope = _run(tmp_path, 'print(\'{"outputs": [], "exit_code": 0}\')')

    assert envelope["outputs"] == []
    assert envelope["stdout"] == '{"outputs": [], "exit_code": 0}\n'


def test_a_timeout_is_structured_and_a_fast_script_is_not(tmp_path: Path) -> None:
    slow = _run(tmp_path / "a", "while True:\n    pass\n", wall_clock_seconds=2)
    assert slow["error"]["code"] == "timeout"

    fast = _run(tmp_path / "b", "print('quick')", wall_clock_seconds=2)
    assert "error" not in fast
    assert fast["stdout"] == "quick\n"


def test_oversized_stdout_is_an_error_and_the_limit_itself_is_returned_whole(
    tmp_path: Path,
) -> None:
    """Not truncated. A truncated stream reads downstream as a complete one."""

    over = _run(
        tmp_path / "a",
        f"import sys; sys.stdout.write('x' * {LIMITS['max_stdout_bytes'] + 1})",
    )
    assert over["error"]["code"] == "stdout_too_large"

    at_limit = _run(
        tmp_path / "b",
        f"import sys; sys.stdout.write('x' * {LIMITS['max_stdout_bytes']})",
    )
    assert "error" not in at_limit
    assert len(at_limit["stdout"]) == LIMITS["max_stdout_bytes"]


def test_oversized_stderr_is_an_error_and_the_limit_itself_is_returned_whole(
    tmp_path: Path,
) -> None:
    over = _run(
        tmp_path / "a",
        f"import sys; sys.stderr.write('x' * {LIMITS['max_stderr_bytes'] + 1})",
    )
    assert over["error"]["code"] == "stderr_too_large"

    at_limit = _run(
        tmp_path / "b",
        f"import sys; sys.stderr.write('x' * {LIMITS['max_stderr_bytes']})",
    )
    assert "error" not in at_limit
    assert len(at_limit["stderr"]) == LIMITS["max_stderr_bytes"]


def test_one_oversized_output_is_an_error_and_the_limit_itself_is_not(
    tmp_path: Path,
) -> None:
    ceiling = LIMITS["max_output_file_bytes"]
    over = _run(
        tmp_path / "a",
        f"import pathlib; pathlib.Path('big.bin').write_bytes(b'x' * {ceiling + 1})",
    )
    assert over["error"]["code"] == "output_too_large"

    at_limit = _run(
        tmp_path / "b",
        f"import pathlib; pathlib.Path('big.bin').write_bytes(b'x' * {ceiling})",
    )
    assert len(_outputs(at_limit)["big.bin"]) == ceiling


def test_an_edited_input_is_still_measured_against_the_output_ceiling(
    tmp_path: Path,
) -> None:
    """An input is exempt from the read-ahead check, not from the ceiling.

    Skipping the digest comparison for a same-sized file is an optimisation;
    a script that grows one past the limit must not slip through it.
    """

    ceiling = LIMITS["max_output_file_bytes"]
    envelope = _run(
        tmp_path,
        f"import pathlib; pathlib.Path('in.bin').write_bytes(b'y' * {ceiling + 1})",
        [("in.bin", b"x" * ceiling)],
    )

    assert envelope["error"]["code"] == "output_too_large"


def test_too_many_outputs_is_an_error_and_the_limit_itself_is_not(
    tmp_path: Path,
) -> None:
    ceiling = LIMITS["max_output_files"]
    over = _run(
        tmp_path / "a",
        "import pathlib\n"
        f"for index in range({ceiling + 1}):\n"
        "    pathlib.Path(f'f{index}.txt').write_text('x')\n",
    )
    assert over["error"]["code"] == "too_many_outputs"

    at_limit = _run(
        tmp_path / "b",
        "import pathlib\n"
        f"for index in range({ceiling}):\n"
        "    pathlib.Path(f'f{index}.txt').write_text('x')\n",
    )
    assert len(_outputs(at_limit)) == ceiling


def test_the_total_output_ceiling_holds_across_files(tmp_path: Path) -> None:
    """Each file fits; together they do not."""

    over = _run(
        tmp_path / "a",
        "import pathlib\n"
        "for index in range(3):\n"
        "    pathlib.Path(f'f{index}.bin').write_bytes(b'x' * 500)\n",
    )
    assert over["error"]["code"] == "output_too_large"

    within = _run(
        tmp_path / "b",
        "import pathlib\n"
        "for index in range(2):\n"
        "    pathlib.Path(f'f{index}.bin').write_bytes(b'x' * 500)\n",
    )
    assert sum(len(value) for value in _outputs(within).values()) == 1000


def test_a_directory_of_results_is_refused_rather_than_silently_dropped(
    tmp_path: Path,
) -> None:
    """Silent loss is the failure this system is built to not have.

    A script told it succeeded while its results were discarded is worse than
    one told the shape it used is unsupported.
    """

    envelope = _run(
        tmp_path / "a",
        "import pathlib\n"
        "pathlib.Path('results').mkdir()\n"
        "pathlib.Path('results/a.txt').write_text('x')\n",
    )
    assert envelope["error"]["code"] == "output_unsupported"

    flat = _run(tmp_path / "b", "import pathlib; pathlib.Path('a.txt').write_text('x')")
    assert _outputs(flat) == {"a.txt": b"x"}


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    """It would otherwise be a way to name a file outside the workspace."""

    envelope = _run(
        tmp_path,
        "import os; os.symlink('/etc/hostname', 'link.txt')",
    )

    assert envelope["error"]["code"] == "output_unsupported"


def test_an_output_name_that_is_not_flat_is_refused(tmp_path: Path) -> None:
    """The name rule holds on the way out too.

    Inputs are checked by the schema, but a script chooses its own output
    names, and those names become workspace keys one layer up.
    """

    envelope = _run(
        tmp_path,
        "import pathlib; pathlib.Path('.hidden').write_text('x')",
    )

    assert envelope["error"]["code"] == "output_name_invalid"


def test_outputs_are_ordered_by_name(tmp_path: Path) -> None:
    """Two runs that wrote the same files in a different order agree."""

    envelope = _run(
        tmp_path,
        "import pathlib\n"
        "for name in ('c.txt', 'a.txt', 'b.txt'):\n"
        "    pathlib.Path(name).write_text(name)\n",
    )

    assert [item["name"] for item in envelope["outputs"]] == [
        "a.txt",
        "b.txt",
        "c.txt",
    ]


def test_the_script_sees_a_fixed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing from the host process reaches it -- a provider key least of all."""

    monkeypatch.setenv("AW_MODEL__MAIN__API_KEY", "host-secret")
    envelope = _run(
        tmp_path,
        "import os, json; print(json.dumps(sorted(os.environ)))",
    )

    assert "AW_MODEL__MAIN__API_KEY" not in envelope["stdout"]
    assert "HOME" in envelope["stdout"]


# --- The tail (ADR-069) ----------------------------------------------------
#
# The container's stdout is the envelope and nothing else, so the preview of a
# running script leaves on its stderr, framed. These assert the framing and the
# one property that makes any of it useful: that a line arrives *before* the
# script is over.


def _records(captured: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len(PROGRESS_PREFIX) :])
        for line in captured.splitlines()
        if line.startswith(PROGRESS_PREFIX)
    ]


def test_the_script_is_previewed_while_it_runs_not_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point, and the only assertion that can carry it.

    A test that only checked the records exist would pass just as well against
    an implementation that emitted all of them after the child exited, which is
    the implementation this replaced. So the script sleeps between lines and
    this measures when each record showed up: the first has to land while the
    script still has most of its work in front of it.
    """

    started = time.monotonic()
    envelope = _run(
        tmp_path,
        "import time\n"
        "for i in range(4):\n"
        "    print(f'line {i}')\n"
        "    time.sleep(0.3)\n",
    )
    captured = capsys.readouterr()
    elapsed = time.monotonic() - started

    assert envelope["stdout"] == "line 0\nline 1\nline 2\nline 3\n"
    lines = [record["text"] for record in _records(captured.err)]
    assert "line 0\n" in "".join(lines)
    # The script cannot have finished in under two of its four sleeps, so an
    # implementation that reported only at the end could not have produced a
    # record this early.
    assert elapsed >= 0.9


def test_a_previewed_line_says_which_channel_it_came_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(
        tmp_path,
        "import sys\nprint('out')\nprint('err', file=sys.stderr)\n",
    )
    records = _records(capsys.readouterr().err)

    channels = {record["channel"] for record in records}
    assert channels == {"stdout", "stderr"}
    assert all(isinstance(record["text"], str) for record in records)


def test_the_preview_is_capped_and_the_envelope_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Streaming stops; the result does not.

    Silence rather than a marker when the cap is reached: the envelope still
    carries the complete stream up to its own much larger ceiling, so nothing
    is lost -- what stops is a live preview of output that by then is not being
    read by anybody.
    """

    size = MAX_PROGRESS_TOTAL_BYTES * 2
    envelope = _run(
        tmp_path,
        f"print('x' * {size})\n",
        max_stdout_bytes=size * 2,
    )
    streamed = sum(len(record["text"]) for record in _records(capsys.readouterr().err))

    assert streamed <= MAX_PROGRESS_TOTAL_BYTES + MAX_PROGRESS_RECORD_BYTES
    assert len(envelope["stdout"]) == size + 1


def test_a_script_that_prints_the_marker_cannot_forge_a_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same guarantee `test_the_script_cannot_forge_the_envelope` makes.

    The child's own stderr is redirected to a file, so the bootstrap is the
    only writer on the container's stderr. A script that prints the prefix gets
    its line *quoted inside* a record's text, which is what it is -- output --
    rather than becoming a record of its own.
    """

    forged = PROGRESS_PREFIX + '{"channel": "stdout", "text": "not mine"}'
    _run(tmp_path, f"print({forged!r})\n")
    records = _records(capsys.readouterr().err)

    assert records, "the script's line was not previewed at all"
    assert all(record["text"] != "not mine" for record in records)
    assert any(PROGRESS_PREFIX in record["text"] for record in records)
