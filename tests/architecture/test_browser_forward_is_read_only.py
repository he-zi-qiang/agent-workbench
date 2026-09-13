"""The control plane forwards one frame from the browser, and only that.

ADR-0113 §3.6 accepted the same cost ADR-095 did, one ADR later: `agent-api`
becomes a client of a process that can drive a browser. What makes that
acceptable is not the intention but the *narrowness* -- one route, one method,
one upstream path, and no way from here to anything that acts on a page.

§4 of that ADR goes further and refuses to let a person drive the browser from
the console at all, because two operators on one page need an arbitration story
that does not exist. That refusal is only real while this module stays a read.
An intention is not checkable and a narrowness is, so this file checks it.

The failure guarded against is not somebody deciding to widen the boundary. It
is somebody adding a "just a click, for debugging" forward without noticing
there was a boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from agent_workbench.apps.api.routes import browser, browser_input

SOURCE: Final[Path] = Path(browser.__file__)

#: The tools that act on the page. Anything in this module naming one of them
#: would be a forward of something other than a read.
ACTING_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "browser_open",
        "browser_eval",
        "browser_interact",
        # `browser_snapshot`, `browser_screenshot` and `browser_diagnostics`
        # are deliberately absent: they *are* reads, and forwarding one would
        # be within this boundary. They are not forwarded today only because
        # the console needs a picture, not a tree -- and because
        # `browser_diagnostics` drains its buffers, so a console polling it
        # would silently eat what the model was about to be shown.
    }
)


def test_the_forward_module_declares_exactly_one_route() -> None:
    """One route, so "the read-only one" is not a claim about which of several."""

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    routes = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "router"
    ]
    assert len(routes) == 1
    [only] = routes
    assert isinstance(only.func, ast.Attribute)
    assert only.func.attr == "get"


def test_the_forward_never_issues_anything_but_a_get() -> None:
    """A POST from here would be an action taken in somebody's browser."""

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for verb in ("post", "put", "patch", "delete", "stream", "send", "request"):
        assert verb not in called, verb


def test_no_acting_tool_is_named_anywhere_in_the_forward() -> None:
    """The upstream also serves tools that open pages and run code in them.

    Naming one here would mean this module had grown a second job.
    """

    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for tool in ACTING_TOOL_NAMES:
        # Docstrings are excluded by checking only non-docstring constants
        # would be fragile; instead the rule is that a tool name may not appear
        # as a value this module computes with.
        acting = {
            literal
            for literal in literals
            if literal == tool and literal not in ast.get_docstring(tree, clean=False)
        }
        assert not acting, tool


def test_the_console_is_never_handed_a_way_to_steer() -> None:
    """ADR-0113 §4: read-only in the strong sense, not merely the HTTP one.

    The panel has no address bar and no clickable surface because this route
    gives it nothing to call. If a request body ever appears here, that has
    stopped being true.
    """

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    annotations = {
        ast.unparse(argument.annotation)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for argument in node.args.args
        if argument.annotation is not None
    }
    # `Request` is Starlette's, and it is how the identity adapter is reached.
    # A pydantic body model would be something else entirely.
    assert annotations <= {"Request"}, annotations


# --- ADR-0117: the person's input has a module of its own, and it is narrow --

INPUT_SOURCE: Final[Path] = Path(browser_input.__file__)


def _computed_literals(path: Path) -> set[str]:
    """String constants a module computes with, its own docstrings aside."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.AsyncFunctionDef | ast.FunctionDef)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def test_the_input_module_names_no_acting_tool_itself() -> None:
    """The person's route computes with none of the acting names.

    ADR-0113 §4's refusal is replaced by a rule, not by an opening: a person
    points and presses in the model's browser, and never opens a page or
    evaluates an expression in it. The one `browser_interact` call lives on the
    slot (`dependencies.py`), behind a method that takes gestures and nothing
    else -- so the route module itself must hold no tool name at all, and a
    second verb would have to be smuggled past both this test and the slot.
    """

    named = ACTING_TOOL_NAMES & _computed_literals(INPUT_SOURCE)
    assert named == set(), named


def test_the_input_module_is_the_only_one_with_a_body() -> None:
    """A pydantic body model is the shape of "something to send"; only here."""

    tree = ast.parse(INPUT_SOURCE.read_text(encoding="utf-8"))
    bodies = {
        ast.unparse(argument.annotation)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for argument in node.args.args
        if argument.annotation is not None
    }
    assert "BrowserInputRequest" in bodies


def test_the_frame_module_is_still_a_read_after_the_input_route_arrived() -> None:
    """The widening went into its own file; the read stayed a read."""

    source = SOURCE.read_text(encoding="utf-8")
    assert "browser_input" not in source
    assert ACTING_TOOL_NAMES & _computed_literals(SOURCE) == set()


def test_a_person_may_open_a_project_file_but_never_name_an_address() -> None:
    """ADR-0120's widening, and its edge.

    A person may put one of their own project files on screen -- the folder
    view and the browser tab were two halves that could not reach each other.
    What they get is "open this file", not an address bar: the request is a
    project id and a relative path, and the `file://` URL is built on the API
    side through the same check-then-join the model's `browser_open` uses. A
    `url` field appearing here is the day that stopped being true.
    """

    fields = set(browser_input.BrowserOpenRequest.model_fields)
    assert fields == {"project_id", "path"}, fields
    assert browser_input.BrowserOpenRequest.model_config.get("extra") == "forbid"
