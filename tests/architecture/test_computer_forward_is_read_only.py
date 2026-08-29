"""The control plane forwards one read to the screen server, and only that.

ADR-095 §5 accepted a cost with its eyes open: `agent-api` becomes a client of a
process that can move the cursor. What makes that acceptable is not the
intention, it is the *narrowness* -- one route, one method, one upstream path,
and no way from here to anything that touches a screen.

An intention is not checkable and a narrowness is, so this file checks it. The
failure it guards against is not somebody deciding to widen the boundary; it is
somebody adding a second forward without noticing there was a boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from agent_workbench.apps.api.routes import computer

SOURCE: Final[Path] = Path(computer.__file__)

#: The tool names that reach a screen. Anything in this module naming one of
#: them would be a forward of something other than a read.
SCREEN_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "request_access",
        "activate_application",
        "screenshot",
        "left_click",
        "type",
        "key",
        "scroll",
        # `list_granted_applications` is deliberately absent: it *is* a read,
        # and forwarding it would be within this boundary. It is not forwarded
        # today only because `/session` answers the same question better.
    }
)


def test_the_forward_module_declares_exactly_one_route() -> None:
    """One route, so "the read-only one" is not a claim about which of several.

    Counted from the decorators rather than from the client, because a second
    route added below the first is exactly the change this file exists to stop
    and exactly the change that reads as harmless in a diff.
    """

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    decorated = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for decorator in node.decorator_list
    ]
    routes = [
        node
        for node in decorated
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "router"
    ]
    assert len(routes) == 1
    [only] = routes
    assert isinstance(only.func, ast.Attribute)
    assert only.func.attr == "get"


def test_the_forward_never_issues_anything_but_a_get() -> None:
    """A POST from here would be an action taken on somebody's screen.

    Asserted against the source rather than by calling it, because the failure
    mode is a line added, not a request observed.
    """

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for verb in ("post", "put", "patch", "delete", "stream", "send", "request"):
        assert verb not in called, verb


def test_no_screen_tool_is_named_anywhere_in_the_forward() -> None:
    """The upstream this talks to also serves eight tools that touch a screen.

    Naming one here would mean this module had grown a second job. Checked as a
    prohibition on the whole file -- a string, a constant, a comment-free
    helper -- because the point is that none of them belongs here at all.
    """

    text = SOURCE.read_text(encoding="utf-8")
    for name in SCREEN_TOOL_NAMES:
        assert name not in text, name


def test_the_forward_reads_its_upstream_from_configuration() -> None:
    """Not a literal, because the literal would escape the loopback validator.

    `api.computer_session_url` is refused at config load when it points anywhere
    but this machine. A URL hard-coded in this module would be a second source
    for the same address with none of that checking -- and the thing being
    addressed is a description of somebody's screen.
    """

    text = SOURCE.read_text(encoding="utf-8")
    assert "computer_session_url" in text
    assert "http://" not in text.split('"""', 2)[-1]
