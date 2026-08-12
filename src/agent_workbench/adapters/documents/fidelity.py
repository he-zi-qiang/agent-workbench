"""Laying a .docx out, for the reader who needs to see it rather than read it.

``docx.py`` recovers a document's text and then counts what it had to drop.
This module answers the other half of the same question: it hands back the
document as the renderer that owns the format would draw it -- fonts, tables
with their rules, page geometry, the pictures the text extraction can only
tally. It does that by converting to PDF and letting the browser's own PDF
viewer draw the result, which is the whole of the mechanism.

**Why an external program rather than a library.** Laying out OOXML is not
something a preview can reimplement: the format's own reference implementations
are word processors. LibreOffice is one, it converts headlessly, and its output
is what somebody opening the file in Word would recognise. The cost is stated
plainly rather than hidden -- this is the first executable this project depends
on that is not a Python package, so a deployment without it has no layout view
at all. That case is not an error here; see ``find_soffice``.

**Why not in the browser.** The alternative was shipping a .docx renderer to
every page load, and it is refused for the reason ``docx.py`` refuses
client-side extraction: it re-derives, in every visitor's tab and against a
zip and an XML tree, something one process here can produce once and cache.

**This module does not parse the document.** The bytes go to LibreOffice as
bytes. The only reading done here is ``preflight_docx``'s, reused rather than
reimplemented so that the ceiling ADR-043 §5 put on every caller holds for this
one too, and so that the repository keeps exactly one docx parsing path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Final

from agent_workbench.adapters.documents.docx import preflight_docx

#: Where a converted document is looked for before it is converted again. The
#: key is the content, so this is safe to share between tenants in the way a
#: hash is: two callers reach the same entry only by holding the same bytes,
#: which each of them was already authorized to read before the route got here.
#: Authorization happens at the route and is not delegated to this name.
_CACHE_DIR_NAME: Final[str] = "agent-workbench-layout-cache"

#: How long a converted PDF stays worth keeping. Deliberately answered rather
#: than left to grow without bound: the cache is a temp directory, a long-lived
#: process would otherwise accumulate one PDF per distinct document forever, and
#: "the disk filled up" is a symptom that arrives far from this file. A day is
#: the same figure the reference implementation settled on, and the reason is
#: the same -- it spans a working session, which is the span over which somebody
#: reopens the same document.
CACHE_TTL_SECONDS: Final[int] = 24 * 60 * 60

#: What one conversion may spend. Measured rather than guessed: on this
#: project's own rendered documents LibreOffice steadies at roughly two seconds,
#: and the very first conversion after installation costs about fourteen while
#: it builds its font caches. Sixty leaves room for that first one and for a
#: document far larger than a preview would be asked for, while still being a
#: bound -- a converter that hangs must fail rather than hold the request open.
SOFFICE_TIMEOUT_SECONDS: Final[float] = 60.0

#: Where LibreOffice lives on macOS when it was installed as an application
#: bundle rather than onto ``PATH``. Checked after ``PATH`` so an explicitly
#: installed binary always wins.
_MACOS_SOFFICE: Final[str] = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class LayoutUnavailableError(RuntimeError):
    """No converter is installed, so this deployment cannot lay a document out.

    Separate from a conversion failure because the two are different facts and
    the reader is owed different sentences: this one says nothing whatever about
    the document, and the same request on a deployment that has LibreOffice
    would succeed. The route maps it to 503, and the console falls back to the
    text preview rather than reporting the document as broken.
    """


class LayoutRenderError(RuntimeError):
    """The converter ran and did not produce a PDF.

    A fact about this document -- it is malformed, or uses something the
    converter refuses -- rather than about the deployment.
    """


def find_soffice() -> str | None:
    """The LibreOffice binary, or ``None`` when this deployment has none.

    Returning ``None`` rather than raising is the point of this function. A
    deployment without LibreOffice is not misconfigured: the text preview is
    intact, the download is unchanged bytes, and the only thing missing is one
    of two views. Making absence an exception would push every caller into
    treating a supported deployment as an error, and the console would show a
    reader a red panel about a document that is perfectly fine.
    """

    found = shutil.which("soffice")
    if found is not None:
        return found
    return _MACOS_SOFFICE if os.path.isfile(_MACOS_SOFFICE) else None


def cache_key(content: bytes, suffix: str) -> str:
    """Content plus format, hashed.

    The suffix is inside the hash rather than only in the filename so that the
    same bytes read as two formats cannot collide on one entry. Separated by a
    NUL because it cannot occur in a suffix, so no pair of (content, suffix)
    can be re-cut into another.
    """

    digest = hashlib.sha256()
    digest.update(content)
    digest.update(b"\0")
    digest.update(suffix.encode("utf-8"))
    return digest.hexdigest()


def _cache_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / _CACHE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _prune_cache(directory: Path, *, now: float) -> None:
    """Drop entries older than the TTL.

    Swept here, on the way past, rather than by a scheduled job: this is a
    cache in a temp directory, and giving it a background task would make a
    convenience into a component with a lifecycle. Failures are ignored on
    purpose -- another process pruning the same directory concurrently is the
    expected case, and losing that race must not fail a preview.
    """

    for entry in directory.glob("*.pdf"):
        try:
            if now - entry.stat().st_mtime > CACHE_TTL_SECONDS:
                entry.unlink()
        except OSError:
            continue


#: One conversion at a time. Not for correctness -- each conversion gets its own
#: LibreOffice profile below, so two could run without colliding -- but for
#: memory: soffice is a word processor, several at once on a small host is how
#: a preview turns into an outage. The queue this creates is bounded by the
#: timeout above.
_conversion_lock: Final[asyncio.Lock] = asyncio.Lock()


async def _run_soffice(binary: str, source: Path, outdir: Path) -> None:
    """Convert ``source`` to PDF in ``outdir``, or raise.

    ``-env:UserInstallation`` is not optional. Without it LibreOffice looks for
    the calling user's profile and, where it cannot bootstrap one -- a service
    account, a container, anything without a writable home -- aborts with "User
    installation could not be completed" and converts nothing, having exited
    non-zero for a reason that reads like a permissions problem. A fresh
    profile per conversion also means two conversions cannot corrupt each
    other's, which is what makes the lock above a resource decision rather than
    a correctness one.

    Started with ``create_subprocess_exec`` rather than run in a thread because
    ADR-042 §6 records that ``asyncio.to_thread`` shares the interpreter's
    default executor with DNS resolution: a two-second conversion parked there
    is two seconds of every other caller's name lookups. A subprocess needs no
    thread at all.
    """

    with tempfile.TemporaryDirectory(prefix="lo_profile_") as profile:
        process = await asyncio.create_subprocess_exec(
            binary,
            f"-env:UserInstallation={Path(profile).as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=SOFFICE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # Killed rather than terminated, and awaited afterwards: a
            # LibreOffice that has stopped responding is not going to honour
            # SIGTERM, and leaving it unreaped would hold the profile directory
            # open past the block that is about to delete it.
            process.kill()
            await process.wait()
            raise LayoutRenderError(
                f"the converter did not finish within {SOFFICE_TIMEOUT_SECONDS:.0f}s"
            ) from None

    if process.returncode != 0:
        tail = output.decode("utf-8", errors="replace").strip()[-500:]
        raise LayoutRenderError(f"the converter exited {process.returncode}: {tail}")


async def render_docx_to_pdf(content: bytes, *, suffix: str = ".docx") -> bytes:
    """The document as a PDF, converted once and then remembered.

    Raises ``LayoutUnavailableError`` when no converter is installed and
    ``LayoutRenderError`` when one ran and produced nothing usable. The
    preflight's ``DocxTooLargeError`` and ``ValueError`` reach the caller
    unchanged, so that a package this process refuses to open is refused the
    same way here as it is on the text path.
    """

    preflight_docx(content)

    binary = find_soffice()
    if binary is None:
        raise LayoutUnavailableError("no LibreOffice binary is installed on this host")

    directory = _cache_dir()
    cached = directory / f"{cache_key(content, suffix)}.pdf"
    if cached.is_file():
        return cached.read_bytes()

    async with _conversion_lock:
        # Re-checked inside the lock: two requests for the same document arrive
        # together often -- a reader opening a panel twice -- and without this
        # the second one converts a document the first has just finished.
        if cached.is_file():
            return cached.read_bytes()

        _prune_cache(directory, now=time.time())
        with tempfile.TemporaryDirectory(prefix="lo_convert_") as work:
            workdir = Path(work)
            source = workdir / f"source{suffix}"
            source.write_bytes(content)
            await _run_soffice(binary, source, workdir)

            produced = source.with_suffix(".pdf")
            if not produced.is_file():
                raise LayoutRenderError(
                    "the converter reported success but wrote no PDF"
                )
            rendered = produced.read_bytes()

        # Written beside the cache and then moved, so a reader never opens a
        # half-written entry: the rename is atomic within one filesystem, and
        # both paths are in the same directory to keep it that way.
        staging = directory / f"{cached.stem}.{os.getpid()}.partial"
        staging.write_bytes(rendered)
        os.replace(staging, cached)
        return rendered


__all__ = [
    "CACHE_TTL_SECONDS",
    "SOFFICE_TIMEOUT_SECONDS",
    "LayoutRenderError",
    "LayoutUnavailableError",
    "cache_key",
    "find_soffice",
    "render_docx_to_pdf",
]
