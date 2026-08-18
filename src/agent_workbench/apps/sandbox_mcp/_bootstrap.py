"""The program that runs *inside* the sandbox container.

It is delivered as source text on the container's command line, because there
is nothing else to deliver it with: ADR-029 §3.2 forbids host mounts and the
container has no network. Its whole job is to turn a JSON payload on stdin into
a JSON envelope on stdout -- materialize the inputs, run the model's script as
a child process, collect what changed, and enforce the output ceilings.

Two properties are worth stating because they are easy to lose:

The child's own stdout and stderr go to files, never to the container's stdout.
The container's stdout carries the envelope and nothing else, so a script that
prints ``{"outputs": []}`` cannot forge a result.

Nothing here imports beyond the standard library, and nothing here knows what a
workspace, a tenant or an owner is. Both are load-bearing: the first is what
lets the image be an unmodified ``python`` base image, the second is ADR-029
§3.1.

This module is importable on the host as well, and its collection and ceiling
logic is tested there directly. Isolation is not testable that way and is not
tested that way -- those assertions run against a real container.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

SANDBOX_ROOT = "/sandbox"

NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

#: What marks a line on the *container's* stderr as a progress record rather
#: than a bootstrap diagnostic (ADR-069).
#:
#: The container's stderr is the only channel available for this. Its stdout is
#: spoken for -- "the envelope and nothing else" is what stops a script that
#: prints ``{"outputs": []}`` from forging a result, and interleaving records
#: there would trade that guarantee for a convenience. Its stderr, by contrast,
#: the child cannot reach at all: the child's own stderr is redirected to a
#: file, so the bootstrap is the sole writer here and a record cannot be forged
#: either.
PROGRESS_PREFIX = "@@sandbox-progress@@ "

#: How often the tail is pumped while the child runs. A quarter second is well
#: under the console's own five-second heartbeat, so the limit on how fresh a
#: line looks is the transport, not this loop.
PROGRESS_POLL_SECONDS = 0.25

#: How much of one channel may go into a single record. A record is a line on
#: a pipe the host reads incrementally; this keeps one `print` of a megabyte
#: from becoming one line of a megabyte.
MAX_PROGRESS_RECORD_BYTES = 2_000

#: How much may be streamed across the whole call, both channels together.
#:
#: Streaming stops silently once this is spent, and silence is the right
#: behaviour here rather than a marker: the envelope still carries the complete
#: streams up to their own much larger ceilings, so nothing is lost -- what
#: stops is the live preview of output that has, by this point, long since
#: stopped being something a person is reading.
MAX_PROGRESS_TOTAL_BYTES = 64_000


def failure(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def execute(payload: dict[str, Any], *, root: str = SANDBOX_ROOT) -> dict[str, Any]:
    """Run one script and return the envelope the host will parse.

    ``root`` is a parameter only so the host-side tests can exercise this on a
    temporary directory. The container always uses the default.
    """

    limits = payload["limits"]
    work = os.path.join(root, "work")
    for directory in (work, os.path.join(root, "home"), os.path.join(root, "tmp")):
        os.makedirs(directory, exist_ok=True)

    entry_sizes: dict[str, int] = {}
    entry_digests: dict[str, str] = {}
    for entry in payload.get("inputs", []):
        content = base64.b64decode(entry["content_base64"])
        with open(os.path.join(work, entry["name"]), "wb") as handle:
            handle.write(content)
        entry_sizes[entry["name"]] = len(content)
        entry_digests[entry["name"]] = hashlib.sha256(content).hexdigest()

    script_path = os.path.join(root, "script.py")
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(payload["script"])

    stdout_path = os.path.join(root, "stdout.bin")
    stderr_path = os.path.join(root, "stderr.bin")
    seconds = limits["wall_clock_seconds"]
    exit_code = _run_child(
        script_path,
        work=work,
        root=root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        seconds=seconds,
    )
    if exit_code is None:
        # Structured, not a crash: a script that loops forever is an ordinary
        # thing for a model to write, and the caller needs to be able to tell
        # it apart from a sandbox that broke.
        return failure(
            "timeout",
            f"the script did not finish within {seconds} seconds",
        )

    for path, channel in ((stdout_path, "stdout"), (stderr_path, "stderr")):
        size = os.path.getsize(path)
        ceiling = limits[f"max_{channel}_bytes"]
        if size > ceiling:
            # Not truncated. A truncated stream is a broken stream that the
            # next step reads as a whole one (ADR-028 §3.4, ADR-029 §3.3).
            return failure(
                f"{channel}_too_large",
                f"the script wrote {size} bytes to {channel}, "
                f"above the {ceiling}-byte limit",
            )

    collected = _collect(work, limits, entry_sizes, entry_digests)
    if "error" in collected:
        return collected

    return {
        "exit_code": exit_code,
        "stdout": _read_text(stdout_path),
        "stderr": _read_text(stderr_path),
        "outputs": collected["outputs"],
    }


def _run_child(
    script_path: str,
    *,
    work: str,
    root: str,
    stdout_path: str,
    stderr_path: str,
    seconds: float,
) -> int | None:
    """Run the model's script, tailing both of its channels while it runs.

    Returns its exit status, or ``None`` if it had to be killed for running
    past ``seconds``.

    This used to be one `subprocess.run(..., timeout=seconds)`, which is
    simpler and returns nothing until the child is over. The tail is why it is
    a poll loop now: the script's output is the only evidence a long run is
    alive, and evidence that arrives with the result is not evidence (ADR-069).

    ``-u`` is load-bearing and easy to lose. Python block-buffers stdout when
    it is a file rather than a terminal, so without it a script that prints a
    line a second writes nothing at all until its 8 KB buffer fills or it
    exits -- the tail would be correct, and would have nothing to read. It is
    a flag rather than ``PYTHONUNBUFFERED`` because ``-I`` implies ``-E`` and
    the child environment is therefore not consulted.
    """

    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", script_path],
            cwd=work,
            env=_child_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
        )
        tail = _ProgressTail(((stdout_path, "stdout"), (stderr_path, "stderr")))
        deadline = time.monotonic() + seconds
        try:
            while True:
                try:
                    status = process.wait(timeout=PROGRESS_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    tail.pump()
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        return None
                    continue
                # One last pump: whatever the child wrote between the final
                # poll and its exit is still on disk and unreported.
                tail.pump()
                return status
        finally:
            tail.close()


class _ProgressTail:
    """The unread end of each channel, emitted as records on stderr.

    Reads from the same files the envelope is later built from, rather than
    from pipes, and that is deliberate: the files stay the single source of
    truth for what the script produced, so a record that is dropped, capped or
    garbled cannot change what the caller finally receives. This is a preview
    of the file, never the file itself.
    """

    def __init__(self, channels: tuple[tuple[str, str], ...]) -> None:
        # These handles outlive __init__ by design -- the whole point
        # is a handle that survives across pumps so each read resumes where
        # the last one stopped. `close()` is called from the runner's
        # `finally`, which is the context manager.
        self._readers = [
            (open(path, "rb"), name)  # noqa: SIM115
            for path, name in channels
        ]
        self._spent = 0

    def pump(self) -> None:
        if self._spent >= MAX_PROGRESS_TOTAL_BYTES:
            return
        for reader, channel in self._readers:
            while True:
                chunk = reader.read(MAX_PROGRESS_RECORD_BYTES)
                if not chunk:
                    break
                self._emit(channel, chunk)
                if self._spent >= MAX_PROGRESS_TOTAL_BYTES:
                    return

    def _emit(self, channel: str, chunk: bytes) -> None:
        self._spent += len(chunk)
        record = {
            "channel": channel,
            # `replace` because a read of a fixed byte count lands mid-sequence
            # for any multi-byte character sooner or later. The envelope
            # decodes the whole file and gets it right; a replacement character
            # in a live preview is the correct trade.
            "text": chunk.decode("utf-8", "replace"),
        }
        try:
            sys.stderr.write(PROGRESS_PREFIX + json.dumps(record) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            # The host stopped reading, or the stream is closed. A preview that
            # cannot be delivered must not take the run down with it -- the
            # envelope is still on its way.
            pass

    def close(self) -> None:
        for reader, _ in self._readers:
            with contextlib.suppress(OSError):
                reader.close()


def _child_environment(root: str) -> dict[str, str]:
    """A fixed environment. Nothing from the host reaches the script.

    ``HOME`` and ``TMPDIR`` point inside the writable layer because the root
    filesystem is read-only: a library that writes a cache into ``$HOME``
    should fail on its own bugs, not on ours.
    """

    return {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin",
        "HOME": os.path.join(root, "home"),
        "TMPDIR": os.path.join(root, "tmp"),
        "MPLCONFIGDIR": os.path.join(root, "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _collect(
    work: str,
    limits: dict[str, Any],
    entry_sizes: dict[str, int],
    entry_digests: dict[str, str],
) -> dict[str, Any]:
    """Everything in the working directory that is not an untouched input.

    Sorted by name so two runs that wrote the same files in a different order
    produce the same envelope. An input the script never touched is not an
    output: returning it would spend the output budget re-sending bytes the
    caller already holds.
    """

    outputs: list[dict[str, Any]] = []
    total = 0
    for name in sorted(os.listdir(work)):
        path = os.path.join(work, name)
        if os.path.islink(path):
            return failure(
                "output_unsupported",
                f"{name!r} is a symbolic link; only regular files are returned",
            )
        if os.path.isdir(path):
            # Refused rather than skipped: a script that put its results in a
            # directory would otherwise be told it succeeded and produced
            # nothing, which is the silent-loss shape this system avoids.
            return failure(
                "output_unsupported",
                f"{name!r} is a directory; the working directory is flat",
            )
        if not os.path.isfile(path):
            return failure(
                "output_unsupported",
                f"{name!r} is not a regular file",
            )

        ceiling = limits["max_output_file_bytes"]
        size = os.path.getsize(path)
        # An untouched input is read back to compare digests, and its size is
        # already bounded by the input ceiling. Anything else must clear the
        # output ceiling before it is read into memory at all.
        may_be_untouched = name in entry_sizes and size == entry_sizes[name]
        if size > ceiling and not may_be_untouched:
            return failure(
                "output_too_large",
                f"{name!r} is {size} bytes, above the {ceiling}-byte per-file limit",
            )
        with open(path, "rb") as handle:
            content = handle.read()
        if (
            may_be_untouched
            and hashlib.sha256(content).hexdigest() == (entry_digests[name])
        ):
            continue
        if size > ceiling:
            return failure(
                "output_too_large",
                f"{name!r} is {size} bytes, above the {ceiling}-byte per-file limit",
            )

        if NAME_PATTERN.match(name) is None:
            return failure(
                "output_name_invalid",
                f"{name!r} is not a flat, printable file name",
            )
        if len(outputs) >= limits["max_output_files"]:
            return failure(
                "too_many_outputs",
                f"the script produced more than {limits['max_output_files']} files",
            )
        total += len(content)
        if total > limits["max_total_output_bytes"]:
            return failure(
                "output_too_large",
                f"the outputs exceed the {limits['max_total_output_bytes']}-byte "
                "total limit",
            )
        outputs.append(
            {
                "name": name,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
            }
        )
    return {"outputs": outputs}


def _read_text(path: str) -> str:
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", errors="replace")


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        envelope = execute(payload)
    except Exception as error:
        # The type, never the message: an exception raised while handling model
        # content routinely carries that content, and this string travels back
        # into the model's context. The detail goes to the container's stderr,
        # which the host logs and no model sees.
        print(f"sandbox bootstrap failed: {error!r}", file=sys.stderr)
        envelope = failure("sandbox_failed", type(error).__name__)
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
