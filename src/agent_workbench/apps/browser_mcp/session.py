"""One browser, driven through CDP, with everything it said kept (ADR-0113).

**Why CDP and not Playwright's own high-level API.** Most of what these tools
need is one protocol command each -- `Accessibility.getFullAXTree`,
`Runtime.evaluate`, `Page.captureScreenshot`, `Input.dispatch*Event` -- and going
through the protocol keeps the mapping between a tool and what it does visible
in one line instead of buried under a convenience layer. Playwright is here for
the parts that are genuinely hard: launching Chromium, its lifecycle, and the
CDP session plumbing.

**`ref_N` is renumbered every snapshot, and that is not a wart.** A ref is an
index into the accessibility tree as it was when it was read. The page moves;
the tree moves with it; a ref that survived would be a promise this module
cannot keep. Handing out fresh ones each time makes the staleness visible
instead of letting a click land on whatever now occupies that slot.

**Nothing here reaches the network by itself.** Chromium is launched pointing at
the guarded proxy with loopback bypass explicitly disabled, so even a request to
127.0.0.1 is judged rather than waved through. `file://` is the exception by
construction: it is not a network request, and it is how a session looks at what
it just wrote.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from agent_workbench.apps.browser_mcp.contract import Action

#: Console and network entries kept between `browser_diagnostics` reads.
LOG_WINDOW: Final[int] = 400
#: One console message, truncated. A page that prints a megabyte per line is
#: not helped by carrying all of it into a tool result.
MAX_LOG_CHARS: Final[int] = 2_000
#: The screencast frame kept for the console panel (ADR-0113 §3.6).
SCREENCAST_QUALITY: Final[int] = 55
SCREENCAST_MAX_WIDTH: Final[int] = 900

VIEWPORT: Final[dict[str, int]] = {"width": 1280, "height": 800}

#: Flags this process always passes.
#:
#: The sandbox is **not** controlled from here, and a previous version of this
#: comment said it was -- "`--no-sandbox` is deliberately absent" was true of
#: this tuple and false of the command line, because Playwright's
#: `chromium_sandbox` defaults to False and adds the flag itself. It is turned
#: on explicitly at the `launch()` call below, where the note records what that
#: cost before it was noticed.
LAUNCH_FLAGS: Final[tuple[str, ...]] = (
    # /dev/shm is 64MB in a default container and Chromium will happily exceed
    # it, producing tab crashes that look like page bugs.
    "--disable-dev-shm-usage",
    # Loopback is bypassed by default, which would let a page reach anything
    # listening inside this container without the proxy ever seeing it.
    "--proxy-bypass-list=<-loopback>",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
)


@dataclass(frozen=True, slots=True)
class OpenOutcome:
    url: str
    title: str
    status: int | None
    console_errors: int


@dataclass(frozen=True, slots=True)
class LogEntry:
    at: float
    level: str
    text: str


class BrowserSession(Protocol):
    """What the server needs; implemented for real and for tests."""

    async def open(self, target: str, timeout_ms: int) -> OpenOutcome: ...
    def workspace_url(self, relative: str) -> str: ...
    async def snapshot(self, max_chars: int) -> str: ...
    async def evaluate(self, expression: str, timeout_ms: int) -> Any: ...
    async def interact(
        self, actions: tuple[Action, ...], timeout_ms: int
    ) -> list[str]: ...
    async def screenshot(self, *, full_page: bool, quality: int) -> bytes: ...
    def drain_logs(self, limit: int) -> tuple[tuple[LogEntry, ...], int]: ...
    def latest_frame(self) -> bytes | None: ...
    async def aclose(self) -> None: ...


@dataclass
class PlaywrightSession:
    """A single Chromium page, kept for the life of the process.

    One page, not a tab strip. A second tab doubles the state a caller has to
    keep straight -- which one a ref belongs to, which one a screenshot shows --
    and nothing in "verify what this session just produced" needs it. Popups are
    refused rather than adopted, for the same reason.
    """

    proxy_endpoint: str
    workspace_root: Path
    headless: bool = True

    _playwright: Any = None
    _browser: Any = None
    _page: Any = None
    _cdp: Any = None
    _logs: deque[LogEntry] = field(default_factory=lambda: deque(maxlen=LOG_WINDOW))
    _dropped: int = 0
    _refs: dict[str, int] = field(default_factory=dict[str, int])
    _frame: bytes | None = None
    #: Strong references to fire-and-forget tasks. Without them the event loop
    #: holds only a weak one and a frame acknowledgement can be collected before
    #: it is sent -- which does not raise, it just stops the screencast.
    _tasks: set[asyncio.Future[Any]] = field(default_factory=set[asyncio.Future[Any]])

    async def start(self) -> None:
        """Launch Chromium. Raises if the `browser` extra is not installed."""

        try:
            import playwright.async_api as pw  # pyright: ignore[reportMissingImports]
        except ImportError as error:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "the browser tools need the `browser` extra: "
                "uv sync --extra browser && playwright install chromium"
            ) from error

        # Bound to a named `Any` right here: the extra is not installed in the
        # gate (like `embedding`, and for the same reason), so without this the
        # checker treats every call below as unknown rather than untyped, and
        # the difference is forty diagnostics about code it cannot see. The
        # module is imported whole for the same reason -- a `from ... import`
        # puts the unknown name on its own line, where the suppression above
        # does not reach it.
        launcher = cast(Any, pw.async_playwright)  # pyright: ignore[reportUnknownMemberType]
        playwright = await launcher().start()
        self._playwright = playwright
        browser = await playwright.chromium.launch(
            headless=self.headless,
            args=list(LAUNCH_FLAGS),
            proxy={"server": self.proxy_endpoint},
            # **Playwright's default is `False`, and it passes `--no-sandbox`.**
            #
            # Leaving `--no-sandbox` out of `LAUNCH_FLAGS` is not the same as
            # not passing it: the flag was going on the command line anyway,
            # from here, while the comment above that tuple said the opposite.
            # Measured 2026-09-12 in a container -- under Docker's *default*
            # seccomp profile, which refuses `CLONE_NEWUSER` and therefore
            # cannot run a sandboxed Chromium, `launch()` started one happily.
            # That is only possible unsandboxed.
            #
            # So the whole of ADR-0113 §3.5 -- the generated seccomp profile,
            # the three deliberate holes, the A/B in that section -- described
            # something this call was opting out of. It cost nothing to write
            # and would have cost the hardest layer in the stack.
            #
            # The failure mode with it on is the one worth having: where the
            # namespace sandbox cannot start, Chromium aborts and this raises,
            # rather than quietly running every page unsandboxed.
            chromium_sandbox=True,
        )
        self._browser = browser
        context = await browser.new_context(viewport=dict(VIEWPORT))
        page = await context.new_page()
        self._page = page
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)

        # A page that opens a window would otherwise become a second page this
        # session does not model.
        def _close_popup(popup: Any) -> None:
            self._spawn(popup.close())

        context.on("page", _close_popup)

        cdp = await context.new_cdp_session(page)
        self._cdp = cdp
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        cdp.on("Page.screencastFrame", self._on_frame)
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": SCREENCAST_QUALITY,
                "maxWidth": SCREENCAST_MAX_WIDTH,
                "everyNthFrame": 2,
            },
        )

    # -- capture -----------------------------------------------------------

    def _record(self, level: str, text: str) -> None:
        if len(self._logs) == self._logs.maxlen:
            self._dropped += 1
        self._logs.append(
            LogEntry(at=time.time(), level=level, text=text[:MAX_LOG_CHARS])
        )

    def _on_console(self, message: Any) -> None:
        self._record(str(message.type), str(message.text))

    def _on_page_error(self, error: Any) -> None:
        self._record("error", str(error))

    def _on_request_failed(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        # A destination the proxy refused arrives here as a failed request; the
        # proxy's own decision log says *why*, and `browser_diagnostics` shows
        # the two together.
        self._record("network", f"{request.method} {request.url} failed: {failure}")

    def _on_frame(self, event: dict[str, Any]) -> None:
        self._frame = base64.b64decode(str(event["data"]))
        session_id = event.get("sessionId")
        cdp = self._cdp
        if session_id is not None and cdp is not None:
            self._spawn(cdp.send("Page.screencastFrameAck", {"sessionId": session_id}))

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def drain_logs(self, limit: int) -> tuple[tuple[LogEntry, ...], int]:
        taken = tuple(self._logs)[-limit:]
        dropped = self._dropped
        self._logs.clear()
        self._dropped = 0
        return taken, dropped

    def latest_frame(self) -> bytes | None:
        return self._frame

    # -- tools -------------------------------------------------------------

    def workspace_url(self, relative: str) -> str:
        """Turn a `workspace_path` into a `file://` URL under the mount.

        **The root is a deployment fact, not a constant.** The first version of
        this wrote `file:///workspace/...` inline in the server, which is where
        the Compose mount happens to be -- so `browser_open` on the native path
        opened a file that does not exist, and `ERR_FILE_NOT_FOUND` was the only
        clue. A root that two places know is a root one of them gets wrong, and
        the one that gets it wrong is the path without a container.

        The containment check is deliberate belt-and-braces: `contract.py`
        already refuses an absolute path and any `..` segment, but it judges
        *text*, and this judges the resolved path -- which is what a symlink
        inside the workspace would change without changing the text.
        """

        root = self.workspace_root.resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"{relative!r} resolves outside the workspace root")
        return target.as_uri()

    async def open(self, target: str, timeout_ms: int) -> OpenOutcome:
        page = self._page
        before = sum(1 for entry in self._logs if entry.level == "error")
        response = await page.goto(target, timeout=timeout_ms)
        after = sum(1 for entry in self._logs if entry.level == "error")
        return OpenOutcome(
            url=str(page.url),
            title=str(await page.title()),
            status=response.status if response is not None else None,
            console_errors=after - before,
        )

    async def snapshot(self, max_chars: int) -> str:
        cdp = self._cdp
        reply = cast(dict[str, Any], await cdp.send("Accessibility.getFullAXTree"))
        raw = cast(list[dict[str, Any]], list(reply.get("nodes") or ()))
        nodes: dict[str, dict[str, Any]] = {str(node["nodeId"]): node for node in raw}
        if not raw:
            return "(the page exposes no accessibility tree)"
        self._refs.clear()
        counter = 0

        def render(node: dict[str, Any], depth: int) -> list[str]:
            nonlocal counter
            if node.get("ignored"):
                lines: list[str] = []
                for child_id in node.get("childIds", ()):
                    child = nodes.get(str(child_id))
                    if child is not None:
                        lines.extend(render(child, depth))
                return lines
            counter += 1
            ref = f"ref_{counter}"
            backend = node.get("backendDOMNodeId")
            if isinstance(backend, int):
                self._refs[ref] = backend
            role = str(_field(node, "role") or "unknown")
            name = str(_field(node, "name") or "")
            value = _field(node, "value")
            line = f"{'  ' * depth}[{ref}] {role}"
            if name:
                line += f': "{name[:200]}"'
            if value not in (None, ""):
                line += f' (value: "{str(value)[:200]}")'
            lines = [line]
            for child_id in node.get("childIds") or ():
                child = nodes.get(str(child_id))
                if child is not None:
                    lines.extend(render(child, depth + 1))
            return lines

        root = raw[0]
        text = "\n".join(render(root, 0))
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (truncated at {max_chars} characters)"
        return text

    async def evaluate(self, expression: str, timeout_ms: int) -> Any:
        # Wrapped as an async function body so `return` and `await` both work,
        # which is what makes a sampling loop over animation frames expressible
        # as one call instead of several.
        page = self._page
        return (
            await asyncio.wait_for(
                page.evaluate(f"async () => {{ {expression} }}"),
                timeout=timeout_ms / 1000,
            ),
        )

    async def interact(self, actions: tuple[Action, ...], timeout_ms: int) -> list[str]:
        done: list[str] = []
        for index, action in enumerate(actions):
            try:
                await asyncio.wait_for(self._perform(action), timeout=timeout_ms / 1000)
            except Exception as error:
                done.append(f"action {index} ({action.kind}) failed: {error}")
                # Stopping is the point: later actions were written assuming
                # this one landed.
                break
            done.append(f"action {index} ({action.kind}) ok")
        return done

    async def _perform(self, action: Action) -> None:
        page = self._page
        if action.kind == "scroll":
            await page.mouse.wheel(0, action.delta_y or 0)
            return
        if action.kind == "key":
            await page.keyboard.press(action.text or "")
            return

        if action.ref is not None:
            x, y = await self._centre_of(action.ref)
            await page.mouse.click(x, y)
        if action.kind == "type":
            await page.keyboard.type(action.text or "")

    async def _centre_of(self, ref: str) -> tuple[float, float]:
        backend = self._refs.get(ref)
        if backend is None:
            raise RuntimeError(
                f"{ref} is not from the most recent snapshot; take a new one"
            )
        cdp = self._cdp
        box = cast(
            dict[str, Any],
            await cdp.send("DOM.getBoxModel", {"backendNodeId": backend}),
        )
        quad = cast(list[float], list(box["model"]["content"]))
        return (quad[0] + quad[4]) / 2, (quad[1] + quad[5]) / 2

    async def screenshot(self, *, full_page: bool, quality: int) -> bytes:
        page = self._page
        return cast(
            bytes,
            await page.screenshot(type="jpeg", quality=quality, full_page=full_page),
        )

    async def aclose(self) -> None:
        closers = cast(tuple[Any, ...], (self._browser, self._playwright))
        for closer in closers:
            if closer is None:
                continue
            with_stop = getattr(closer, "stop", None) or closer.close
            # Shutdown is best effort: a browser that already died takes its
            # transport with it, and raising here would mask whatever actually
            # stopped the process.
            with contextlib.suppress(Exception):
                await with_stop()


def _field(node: dict[str, Any], key: str) -> Any:
    """Unwrap CDP's `{"value": ...}` envelope, which is absent as often as not."""

    wrapper = node.get(key)
    if not isinstance(wrapper, dict):
        return None
    return cast(dict[str, Any], wrapper).get("value")


def render_logs(entries: tuple[LogEntry, ...], dropped: int) -> str:
    """The diagnostics projection, including what fell out of the window."""

    lines = [f"[{entry.level}] {entry.text}" for entry in entries]
    if dropped:
        lines.append(f"... ({dropped} earlier entries dropped from the window)")
    return "\n".join(lines) or "(nothing reported since the last read)"


def describe(value: Any) -> str:
    """A tool result body for whatever `browser_eval` produced."""

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


__all__ = [
    "LAUNCH_FLAGS",
    "VIEWPORT",
    "BrowserSession",
    "LogEntry",
    "OpenOutcome",
    "PlaywrightSession",
    "describe",
    "render_logs",
]
