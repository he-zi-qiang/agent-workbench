"""One container per call, and the flags that make that a pure function.

ADR-029's whole argument rests on the container being unable to reach anything
outside itself. That is not a property of the code below so much as a property
of the flags it passes, which is why they are constants here and not
configuration: a deployment that could turn ``--network=none`` off would be a
deployment where every replay guarantee in this system is void, and ADR-029 §3.2
is explicit that the network switch is the premise rather than a hardening
extra.

The same reasoning covers the rest of :data:`ISOLATION_FLAGS` -- read-only root,
a non-executable tmpfs writable layer, non-root user, no capabilities, no
privilege escalation, no host mounts, and ceilings on memory, CPU, processes
and wall clock.

Nothing is mounted from the host. The payload goes in on stdin and the envelope
comes back on stdout, which is also why :mod:`._bootstrap` is delivered as
source text on the command line: there is no other channel.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from pydantic import TypeAdapter, ValidationError

from agent_workbench.apps.sandbox_mcp._bootstrap import PROGRESS_PREFIX
from agent_workbench.apps.sandbox_mcp.contract import (
    SandboxFile,
    SandboxRequest,
    base64_length,
)
from agent_workbench.domain.schema import JsonObject

logger = logging.getLogger(__name__)

#: How a caller receives the script's output while the script is still running:
#: ``(channel, text)``, where ``channel`` is ``"stdout"`` or ``"stderr"``.
OutputSink = Callable[[str, str], Awaitable[None]]

_PROGRESS_PREFIX_BYTES: Final[bytes] = PROGRESS_PREFIX.encode("utf-8")

#: When a partial line is given up on and filed as diagnostic text. Two orders
#: of magnitude above the largest record `_bootstrap` will emit, so this path
#: is only ever reached by something that is genuinely not a record.
_PENDING_FLUSH_BYTES: Final[int] = 256 * 1024

#: A stock image, named rather than built. The sandbox needs an interpreter and
#: nothing else; a project-built image would add this repository's own code to
#: the one place it must not be reachable from.
DEFAULT_SANDBOX_IMAGE: Final[str] = "python:3.12-slim"
DEFAULT_CONTAINER_RUNTIME: Final[str] = "docker"

#: How long the model's script may run. Enforced twice: :mod:`._bootstrap` kills
#: the child at this figure and returns a structured timeout, and the host kills
#: the container at this figure plus the grace below. The second is the backstop
#: for the cases the first cannot reach, a fork bomb among them.
WALL_CLOCK_SECONDS: Final[int] = 60
CONTAINER_GRACE_SECONDS: Final[int] = 15

MEMORY_LIMIT: Final[str] = "512m"
CPU_LIMIT: Final[str] = "1.0"
PIDS_LIMIT: Final[int] = 64
TMPFS_SIZE: Final[str] = "256m"

MAX_STDOUT_BYTES: Final[int] = 256 * 1024
MAX_STDERR_BYTES: Final[int] = 256 * 1024
MAX_OUTPUT_FILES: Final[int] = 32
MAX_OUTPUT_FILE_BYTES: Final[int] = 4 * 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES: Final[int] = 16 * 1024 * 1024

#: What the host will read off the container's stdout before giving up. The
#: envelope is JSON with base64 payloads, so it is larger than the byte
#: ceilings it carries; the slack covers the field names and the escaping.
MAX_ENVELOPE_BYTES: Final[int] = (
    base64_length(MAX_TOTAL_OUTPUT_BYTES)
    + 2 * (MAX_STDOUT_BYTES + MAX_STDERR_BYTES)
    + MAX_OUTPUT_FILES * 512
    + 4096
)

#: Everything ADR-029 §3.2 writes down, in the form the runtime takes it.
ISOLATION_FLAGS: Final[tuple[str, ...]] = (
    # The premise. Without it nothing below matters and ADR-029 does not hold.
    "--network=none",
    "--read-only",
    # `noexec` alongside the other two. **Measured: this is a no-op on Docker**,
    # which applies noexec to every `--tmpfs` by default -- reading
    # /proc/mounts inside the container shows it with or without this word. It
    # is written anyway for the reason the whole tuple is a constant rather
    # than configuration: what this module states is the guarantee, and a
    # guarantee that is really a runtime's default is one that changes when
    # somebody swaps the runtime. The assertion that matters is in
    # `tests/apps/test_sandbox_isolation.py`, which runs the attempt.
    #
    # It costs nothing: the model's script is handed to an interpreter as an
    # argument (`_bootstrap` runs `[sys.executable, "-I", script_path]`), so
    # nothing in this directory is ever executed as a program. What noexec
    # removes is the step after that -- a script that writes a binary into its
    # own writable layer, marks it executable and runs it directly.
    f"--tmpfs=/sandbox:rw,nosuid,nodev,noexec,mode=1777,size={TMPFS_SIZE}",
    # 65534 is `nobody` in every base image this could plausibly run on, and
    # the tmpfs above is world-writable, so the script needs no account of its
    # own to have somewhere to work.
    "--user=65534:65534",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    f"--memory={MEMORY_LIMIT}",
    # Equal to --memory, which is how this runtime spells "no swap". Without
    # it the memory ceiling is a ceiling on residency, not on consumption.
    f"--memory-swap={MEMORY_LIMIT}",
    f"--cpus={CPU_LIMIT}",
    f"--pids-limit={PIDS_LIMIT}",
    "--workdir=/sandbox",
)

_BOOTSTRAP_SOURCE: Final[str] = (
    Path(__file__).with_name("_bootstrap.py").read_text(encoding="utf-8")
)

#: The envelope is untrusted the same way any other subprocess output is: it is
#: parsed against a shape, not trusted for being ours.
_ENVELOPE: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)


class SandboxExecutionError(RuntimeError):
    """The run did not produce a usable envelope.

    ``code`` is a stable, non-echoing label the server turns into a protocol
    error. The message names the limit or the failure mode and never the
    script, the file contents, or anything about the host.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    """What one execution produced. A non-zero ``exit_code`` is not an error.

    A script that raises is a normal result the caller must be able to read:
    the traceback is in ``stderr`` and is usually the most useful thing the
    call returns.
    """

    exit_code: int
    stdout: str
    stderr: str
    outputs: tuple[SandboxFile, ...]


@dataclass(frozen=True, slots=True)
class SandboxExecutor:
    """Runs one script in one throwaway container.

    ``runtime`` and ``image`` say what a deployment has installed. Neither
    weakens anything: the isolation is in :data:`ISOLATION_FLAGS`, which this
    class always passes and nothing can reach.

    ``wall_clock_seconds`` is the one ceiling exposed here, and it is a test
    seam rather than a knob. A timeout that can only be asserted by waiting
    :data:`WALL_CLOCK_SECONDS` is a timeout no suite exercises, and an
    unexercised kill path is the one that turns out not to kill. There is
    deliberately no command-line flag and no configuration field for it --
    ``tests/apps/test_sandbox_mcp_server.py`` pins that absence.
    """

    runtime: str = DEFAULT_CONTAINER_RUNTIME
    image: str = DEFAULT_SANDBOX_IMAGE
    wall_clock_seconds: int = WALL_CLOCK_SECONDS

    async def probe(self) -> bool:
        """Whether the container runtime is present and answering.

        A binary on ``PATH`` is not enough -- the common failure is an
        installed client whose daemon is not running, and that reports as an
        available runtime right up until the first call.
        """

        if shutil.which(self.runtime) is None:
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                self.runtime,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            return await asyncio.wait_for(process.wait(), timeout=10) == 0
        except TimeoutError:
            process.kill()
            await process.wait()
            return False

    async def run(
        self,
        request: SandboxRequest,
        *,
        on_output: OutputSink | None = None,
    ) -> SandboxOutcome:
        """Execute one request, or raise :class:`SandboxExecutionError`.

        ``on_output`` receives ``(channel, text)`` for each slice of the
        script's own stdout or stderr as it is produced, where ``channel`` is
        ``"stdout"`` or ``"stderr"`` (ADR-069). It is a preview and never the
        result: the envelope this returns carries the complete streams, and a
        caller that ignores this argument sees exactly what it saw before.

        Delivery is best-effort in one specific way worth naming -- a sink that
        raises is dropped and the run continues. The container is already
        running by then, and taking a live script down because nobody could be
        told about its output would be the observation destroying the thing
        observed.
        """

        container = f"agent-workbench-sandbox-{uuid.uuid4().hex}"
        payload = json.dumps(
            {
                "script": request.script,
                "inputs": [
                    {
                        "name": file.name,
                        "content_base64": _b64(file.content),
                    }
                    for file in request.inputs
                ],
                "limits": {
                    "wall_clock_seconds": self.wall_clock_seconds,
                    "max_stdout_bytes": MAX_STDOUT_BYTES,
                    "max_stderr_bytes": MAX_STDERR_BYTES,
                    "max_output_files": MAX_OUTPUT_FILES,
                    "max_output_file_bytes": MAX_OUTPUT_FILE_BYTES,
                    "max_total_output_bytes": MAX_TOTAL_OUTPUT_BYTES,
                },
            }
        ).encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                self.runtime,
                "run",
                "--rm",
                "--interactive",
                f"--name={container}",
                *ISOLATION_FLAGS,
                "--entrypoint=python3",
                self.image,
                "-I",
                "-c",
                _BOOTSTRAP_SOURCE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxExecutionError(
                "sandbox_unavailable", "the container runtime could not be started"
            ) from error

        deadline = self.wall_clock_seconds + CONTAINER_GRACE_SECONDS
        try:
            stdout, stdout_total, stderr, _ = await asyncio.wait_for(
                self._pump(process, payload, on_output),
                timeout=deadline,
            )
        except TimeoutError:
            # The bootstrap's own timeout should have fired first. Reaching
            # here means the container could not report for itself, so the
            # container is what has to be removed.
            await self._force_remove(container)
            process.kill()
            await process.wait()
            raise SandboxExecutionError(
                "timeout",
                f"the sandbox container did not finish within {deadline} seconds",
            ) from None

        if stdout_total > MAX_ENVELOPE_BYTES:
            raise SandboxExecutionError(
                "output_too_large", "the sandbox result exceeds the transfer ceiling"
            )
        if not stdout:
            # The runtime's own diagnosis -- a missing image, a daemon that
            # refused -- is an operator's problem and often names host paths.
            # It goes to the log; the model is told only that nothing came back.
            logger.error(
                "sandbox container produced no result: %s", _first_line(stderr)
            )
            raise SandboxExecutionError(
                "sandbox_failed", "the sandbox produced no result"
            )
        return _parse_envelope(stdout)

    async def _pump(
        self,
        process: asyncio.subprocess.Process,
        payload: bytes,
        on_output: OutputSink | None = None,
    ) -> tuple[bytes, int, bytes, int]:
        """Feed stdin and drain both pipes concurrently.

        Concurrently because the payload can be tens of megabytes: writing it
        all before reading would deadlock the moment the container answers
        before it has consumed its input.
        """

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdin = process.stdin

        async def write() -> None:
            try:
                stdin.write(payload)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The container exited before reading its input. Whatever it
                # said on stdout or stderr is the real diagnosis.
                pass
            finally:
                stdin.close()

        _, out, err = await asyncio.gather(
            write(),
            _read_capped(process.stdout, MAX_ENVELOPE_BYTES),
            # stderr, not stdout, is where the progress records are: the
            # container's stdout is the envelope and nothing else, which is
            # what stops a script from forging a result (`_bootstrap`).
            _read_records(process.stderr, MAX_STDERR_BYTES, on_output),
        )
        await process.wait()
        return out[0], out[1], err[0], err[1]

    async def _force_remove(self, container: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.runtime,
                "rm",
                "--force",
                container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()


async def _read_records(
    stream: asyncio.StreamReader,
    cap: int,
    on_output: OutputSink | None,
) -> tuple[bytes, int]:
    """Drain stderr, dispatching progress records and keeping the rest.

    Same contract as :func:`_read_capped` for what it returns -- the kept bytes
    and the true total -- so the caller's diagnostics are unchanged. What it
    adds is that a line carrying `_bootstrap.PROGRESS_PREFIX` is handed to
    ``on_output`` and **not** kept: a record is this transport's framing, not
    something the script wrote, and leaving it in would put the framing into
    the operator log that `_first_line` reads.

    Lines are assembled here rather than with `readline()` because that method
    raises once a line passes the reader's internal limit, and stderr can carry
    a runtime's multi-kilobyte diagnostic on one line. Records are bounded far
    below `_PENDING_FLUSH_BYTES`, so a record never reaches that path.
    """

    chunks: list[bytes] = []
    kept = 0
    total = 0
    pending: bytes = b""

    def keep(data: bytes) -> None:
        nonlocal kept, total
        total += len(data)
        if kept < cap:
            take = data[: cap - kept]
            chunks.append(take)
            kept += len(take)

    async def take_line(line: bytes) -> None:
        record = _progress_record(line)
        if record is None:
            keep(line)
            return
        if on_output is None:
            return
        channel, text = record
        try:
            await on_output(channel, text)
        except Exception:
            # A sink that raises is dropped. See `run`: the container is
            # already executing, and a failed preview must not end it.
            logger.debug("sandbox progress sink raised", exc_info=True)

    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        pending += chunk
        while True:
            # Sliced rather than unpacked from `split`/`partition`. Both of
            # those reassign `pending` from a call on `pending` itself, which
            # makes the inference circular and leaves the name partially typed;
            # a slice of bytes is bytes with nothing to infer.
            break_at = pending.find(b"\n")
            if break_at < 0:
                break
            await take_line(pending[: break_at + 1])
            pending = pending[break_at + 1 :]
        if len(pending) >= _PENDING_FLUSH_BYTES:
            # A single line longer than any record can be. It is diagnostic
            # text, so it is kept rather than held for a newline that may
            # never arrive.
            keep(pending)
            pending = b""
    if pending:
        await take_line(pending)
    return b"".join(chunks), total


def _progress_record(line: bytes) -> tuple[str, str] | None:
    """The ``(channel, text)`` a record carries, or ``None`` if it is not one.

    Every failure to read a well-formed record returns ``None``, which files
    the line as ordinary stderr. That is the safe direction: a malformed record
    shown to an operator as a strange log line is recoverable, where a
    malformed record dispatched as script output would put this transport's
    own framing in front of a reader as though the script had printed it.
    """

    if not line.startswith(_PROGRESS_PREFIX_BYTES):
        return None
    try:
        body = json.loads(line[len(_PROGRESS_PREFIX_BYTES) :])
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    record = cast(dict[str, Any], body)
    channel = record.get("channel")
    text = record.get("text")
    if channel not in ("stdout", "stderr") or not isinstance(text, str):
        return None
    return channel, text


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, int]:
    """Read to EOF, keeping at most ``cap`` bytes and reporting the true size.

    Draining past the cap rather than stopping: a reader that walks away leaves
    the writer blocked on a full pipe, and the caller needs the real total to
    tell "large" from "at the limit".
    """

    chunks: list[bytes] = []
    kept = 0
    total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return b"".join(chunks), total
        total += len(chunk)
        if kept < cap:
            keep = chunk[: cap - kept]
            chunks.append(keep)
            kept += len(keep)


def _parse_envelope(raw: bytes) -> SandboxOutcome:
    try:
        body = _ENVELOPE.validate_json(raw)
    except ValidationError as error:
        raise SandboxExecutionError(
            "sandbox_failed", "the sandbox returned a malformed result"
        ) from error

    refusal = body.get("error")
    if isinstance(refusal, dict):
        raise SandboxExecutionError(
            str(refusal.get("code", "sandbox_failed")),
            str(refusal.get("message", "the sandbox refused the run")),
        )

    try:
        outputs = tuple(
            SandboxFile(
                name=str(cast(dict[str, Any], item)["name"]),
                content=base64.b64decode(
                    str(cast(dict[str, Any], item)["content_base64"]), validate=True
                ),
            )
            for item in cast(list[Any], body["outputs"])
        )
        return SandboxOutcome(
            exit_code=int(cast(int, body["exit_code"])),
            stdout=str(body["stdout"]),
            stderr=str(body["stderr"]),
            outputs=outputs,
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise SandboxExecutionError(
            "sandbox_failed", "the sandbox returned a malformed result"
        ) from error


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _first_line(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip().splitlines()
    return text[0][:200] if text else ""


__all__ = [
    "CONTAINER_GRACE_SECONDS",
    "DEFAULT_CONTAINER_RUNTIME",
    "DEFAULT_SANDBOX_IMAGE",
    "ISOLATION_FLAGS",
    "MAX_ENVELOPE_BYTES",
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "MAX_TOTAL_OUTPUT_BYTES",
    "WALL_CLOCK_SECONDS",
    "SandboxExecutionError",
    "SandboxExecutor",
    "SandboxOutcome",
]
