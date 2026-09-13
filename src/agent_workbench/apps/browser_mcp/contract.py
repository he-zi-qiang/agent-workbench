"""Closed, bounded input contracts for the six browser tools (ADR-0113 §3.4).

Six tools, not the twenty a general browsing agent gets, because this surface is
sized by one question: *what does verifying a page once actually need?* The
answer is open it, read its structure, run something in it, poke it, look at it,
and ask what went wrong -- and `browser_eval` is the one that turns "it renders"
into a number you can check against a spec.

Every schema here is `additionalProperties: false` with an explicit ceiling on
anything that can grow. A tool call is model-authored input arriving over a
transport, and the cheapest place to refuse a malformed one is before it becomes
a Playwright call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from pydantic import TypeAdapter, ValidationError

#: A model-authored expression. Generous enough for a real assertion helper --
#: a sampling loop over `requestAnimationFrame`, say -- and far below anything
#: that would make a JSON-RPC frame awkward.
MAX_EXPRESSION_CHARS: Final[int] = 20_000
#: One `browser_open` target.
MAX_URL_CHARS: Final[int] = 4_096
#: Actions per `browser_interact`. A batch exists to save round trips, not to
#: become a scripting language; past this, write the loop in `browser_eval`.
MAX_ACTIONS: Final[int] = 32
MAX_TEXT_CHARS: Final[int] = 4_096
#: Milliseconds a single tool may wait on the page.
MAX_TIMEOUT_MS: Final[int] = 60_000
DEFAULT_TIMEOUT_MS: Final[int] = 15_000

_JSON_OBJECT: Final[TypeAdapter[dict[str, Any]]] = TypeAdapter(dict[str, Any])


class BrowserInputError(ValueError):
    """A tool call this server refuses before it reaches the browser."""


OPEN_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Open a page and wait for it to load. Give either `url` for an "
        "http/https address, or `workspace_path` for a file this session "
        "produced -- the second is how you look at something you just wrote. "
        "Every request the page then makes, including ones its own scripts "
        "make, is judged by this deployment's destination guard; a refused "
        "one appears in `browser_diagnostics` rather than failing silently."
    ),
    "properties": {
        "url": {"type": "string", "maxLength": MAX_URL_CHARS},
        "workspace_path": {"type": "string", "maxLength": 512},
        "timeout_ms": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TIMEOUT_MS,
            "description": f"Default {DEFAULT_TIMEOUT_MS}.",
        },
    },
}

SNAPSHOT_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "The page's accessibility tree, each interactive element tagged "
        "[ref_N] for `browser_interact`. Prefer this over a screenshot for "
        "checking text, structure and control state: it costs a fraction of "
        "the tokens and you can assert on it. Refs are renumbered by every "
        "snapshot, so use the ones you just received."
    ),
    "properties": {
        "max_chars": {"type": "integer", "minimum": 200, "maximum": 200_000},
    },
}

EVAL_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["expression"],
    "description": (
        "Evaluate JavaScript in the page and get the result back as JSON. "
        "This is how you check whether something is *correct* rather than "
        "merely rendered: read the running state, sample it over frames, "
        "compare it against what the specification says it should be. The "
        "expression is an async function body -- use `return`, and `await` "
        "works. Anything not JSON-serialisable comes back as a description."
    ),
    "properties": {
        "expression": {"type": "string", "maxLength": MAX_EXPRESSION_CHARS},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_MS},
    },
}

_ACTION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["click", "type", "key", "scroll"]},
        "ref": {
            "type": "string",
            "maxLength": 32,
            "description": "A [ref_N] from the most recent browser_snapshot.",
        },
        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
        "delta_y": {"type": "integer", "minimum": -20_000, "maximum": 20_000},
        # A point in the viewport, for a click that has no ref to name
        # (ADR-0117): the console panel forwards a person's click on the
        # frame, and a person points at pixels, not at snapshot refs. The
        # model keeps using refs -- a point is meaningless to something that
        # reads the page as a tree.
        "x": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "y": {"type": "integer", "minimum": 0, "maximum": 10_000},
    },
}

INTERACT_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "description": (
        "Run a short batch of input actions in order, stopping at the first "
        "failure. `click` needs a ref; `type` needs text and usually a ref; "
        "`key` sends one key name such as Enter or Tab; `scroll` takes "
        "delta_y. Take a fresh snapshot afterwards -- refs from before the "
        "batch may no longer mean anything."
    ),
    "properties": {
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ACTIONS,
            "items": _ACTION_SCHEMA,
        },
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_MS},
    },
}

SCREENSHOT_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "A JPEG of the viewport. Use it when the answer is genuinely visual -- "
        "layout, overlap, colour, an image that did or did not load. For text, "
        "state and structure, `browser_snapshot` is cheaper and assertable."
    ),
    "properties": {
        "full_page": {"type": "boolean"},
        "quality": {"type": "integer", "minimum": 20, "maximum": 95},
    },
}

DIAGNOSTICS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "What the page reported since the last read: console messages, "
        "uncaught exceptions, failed requests, and every destination the "
        "guard refused. Reading drains the buffers and reports how many "
        "entries fell out of the window, so a quiet answer means quiet and "
        "not truncated."
    ),
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
}


@dataclass(frozen=True, slots=True)
class OpenRequest:
    url: str | None
    workspace_path: str | None
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class EvalRequest:
    expression: str
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class Action:
    kind: Literal["click", "type", "key", "scroll"]
    ref: str | None
    text: str | None
    delta_y: int | None
    #: A viewport point, the other way to say where a click lands
    #: (ADR-0117). Both or neither; a click carries a ref or a point.
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True, slots=True)
class InteractRequest:
    actions: tuple[Action, ...]
    timeout_ms: int


def parse_open(arguments: dict[str, Any]) -> OpenRequest:
    payload = _object(arguments)
    _reject_unknown(payload, OPEN_INPUT_SCHEMA)
    url = _optional_string(payload, "url", MAX_URL_CHARS)
    path = _optional_string(payload, "workspace_path", 512)
    # Exactly one, because "both" has no defensible meaning and "neither" is a
    # call that could only have been a mistake.
    if (url is None) == (path is None):
        raise BrowserInputError("give exactly one of url or workspace_path")
    if path is not None and (path.startswith("/") or ".." in path.split("/")):
        # The path is joined onto the session's own directory. An absolute path
        # or a parent segment would leave it, and the mount is read-only rather
        # than empty, so leaving it is worth refusing here in words the model
        # can act on.
        raise BrowserInputError(
            "workspace_path must be relative and must not contain '..'"
        )
    return OpenRequest(url=url, workspace_path=path, timeout_ms=_timeout(payload))


def parse_eval(arguments: dict[str, Any]) -> EvalRequest:
    payload = _object(arguments)
    _reject_unknown(payload, EVAL_INPUT_SCHEMA)
    expression = payload.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise BrowserInputError("expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise BrowserInputError(f"expression exceeds {MAX_EXPRESSION_CHARS} characters")
    return EvalRequest(expression=expression, timeout_ms=_timeout(payload))


def parse_interact(arguments: dict[str, Any]) -> InteractRequest:
    payload = _object(arguments)
    _reject_unknown(payload, INTERACT_INPUT_SCHEMA)
    given: Any = payload.get("actions")
    if not isinstance(given, list) or not given:
        raise BrowserInputError("actions must be a non-empty array")
    raw = cast(list[Any], given)
    if len(raw) > MAX_ACTIONS:
        raise BrowserInputError(f"at most {MAX_ACTIONS} actions per call")

    actions: list[Action] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BrowserInputError(f"action {index} is not an object")
        entry = cast(dict[str, Any], item)
        kind: Any = entry.get("kind")
        if kind not in ("click", "type", "key", "scroll"):
            raise BrowserInputError(f"action {index} has an unknown kind {kind!r}")
        ref = _optional_string(entry, "ref", 32)
        text = _optional_string(entry, "text", MAX_TEXT_CHARS)
        delta: Any = entry.get("delta_y")
        x: Any = entry.get("x")
        y: Any = entry.get("y")
        point = isinstance(x, int) and isinstance(y, int)
        if (x is None) != (y is None):
            raise BrowserInputError(f"action {index}: a point needs both x and y")
        if kind == "click" and ref is None and not point:
            raise BrowserInputError(f"action {index}: click needs a ref or a point")
        if kind in ("type", "key") and text is None:
            raise BrowserInputError(f"action {index}: {kind} needs text")
        if kind == "scroll" and not isinstance(delta, int):
            raise BrowserInputError(f"action {index}: scroll needs delta_y")
        actions.append(
            Action(
                kind=kind,
                ref=ref,
                text=text,
                delta_y=delta if isinstance(delta, int) else None,
                x=x if point else None,
                y=y if point else None,
            )
        )
    return InteractRequest(actions=tuple(actions), timeout_ms=_timeout(payload))


def _object(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _JSON_OBJECT.validate_python(arguments, strict=True)
    except ValidationError as error:
        raise BrowserInputError("arguments must be a JSON object") from error


def _reject_unknown(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    unknown = set(payload) - set(schema["properties"])
    if unknown:
        raise BrowserInputError("unknown field(s): " + ", ".join(sorted(unknown)))


def _optional_string(payload: dict[str, Any], key: str, ceiling: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BrowserInputError(f"{key} must be a string")
    if len(value) > ceiling:
        raise BrowserInputError(f"{key} exceeds {ceiling} characters")
    return value


def _timeout(payload: dict[str, Any]) -> int:
    value = payload.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BrowserInputError("timeout_ms must be an integer")
    if not 1 <= value <= MAX_TIMEOUT_MS:
        raise BrowserInputError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    return value


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "DIAGNOSTICS_INPUT_SCHEMA",
    "EVAL_INPUT_SCHEMA",
    "INTERACT_INPUT_SCHEMA",
    "MAX_ACTIONS",
    "MAX_EXPRESSION_CHARS",
    "OPEN_INPUT_SCHEMA",
    "SCREENSHOT_INPUT_SCHEMA",
    "SNAPSHOT_INPUT_SCHEMA",
    "Action",
    "BrowserInputError",
    "EvalRequest",
    "InteractRequest",
    "OpenRequest",
    "parse_eval",
    "parse_interact",
    "parse_open",
]
