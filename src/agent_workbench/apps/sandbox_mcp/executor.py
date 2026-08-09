"""One container per call, and the flags that make that a pure function.

ADR-029's whole argument rests on the container being unable to reach anything
outside itself. That is not a property of the code below so much as a property
of the flags it passes, which is why they are constants here and not
configuration: a deployment that could turn ``--network=none`` off would be a
deployment where every replay guarantee in this system is void, and ADR-029 §3.2
is explicit that the network switch is the premise rather than a hardening
extra.

The same reasoning covers the rest of :data:`ISOLATION_FLAGS` -- read-only root,
tmpfs writable layer, non-root user, no capabilities, no privilege escalation,
no host mounts, and ceilings on memory, CPU, processes and wall clock.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from pydantic import TypeAdapter, ValidationError

from agent_workbench.apps.sandbox_mcp.contract import (
    SandboxFile,
    SandboxRequest,
    base64_length,
)
from agent_workbench.domain.schema import JsonObject

logger = logging.getLogger(__name__)

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
    f"--tmpfs=/sandbox:rw,nosuid,nodev,mode=1777,size={TMPFS_SIZE}",
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

    async def run(self, request: SandboxRequest) -> SandboxOutcome:
        """Execute one request, or raise :class:`SandboxExecutionError`."""

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
                self._pump(process, payload),
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
            _read_capped(process.stderr, MAX_STDERR_BYTES),
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
