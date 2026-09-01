"""The one guarantee the key route makes with no authentication behind it.

ADR-101 accepts that anything reaching this loopback port can *set* the model
key, because ADR-044 already says the Identity Adapter trusts request headers.
What it does not accept is that reaching the port should hand over the key that
is already there -- and the only reason that holds is that no code path returns
one.

That is a structural property, so it is asserted structurally. The behavioural
half lives in ``tests/api/test_provider_key_api.py``, which drives real requests
against a real store and greps the responses; this file guards the shape that
makes the behaviour hard to break by accident. Both, because either alone is
weak: a field added to the response model would slip past a test that only
checks today's payloads, and a handler that read the file and logged it would
slip past a test that only checks the model.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE_TYPES = ROOT / "web" / "src" / "api" / "types.ts"
ROUTE = ROOT / "src" / "agent_workbench" / "apps" / "api" / "routes" / "settings.py"
SERVICE = ROOT / "src" / "agent_workbench" / "application" / "provider_key.py"

#: Every field the console is allowed to be told. Adding to this set is the
#: deliberate act; the point of the test is that it cannot happen by accident.
ALLOWED_VIEW_FIELDS = {
    "active",
    "stored",
    "fingerprint",
    "path",
    "restart_required",
    "restart_hint",
}


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def test_the_response_model_has_no_field_that_could_carry_a_key() -> None:
    """A new field on the view is the cheapest way to leak this by accident."""
    view = _class(ast.parse(ROUTE.read_text(encoding="utf-8")), "ProviderKeyView")
    fields = {
        node.target.id
        for node in view.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == ALLOWED_VIEW_FIELDS, (
        "a field was added to the provider-key response. If it can carry key "
        "material, ADR-101 §3.1 says no; if it cannot, add it to "
        "ALLOWED_VIEW_FIELDS here and say why in the ADR."
    )


def test_the_service_exposes_no_way_to_read_the_key_back() -> None:
    """`read` is internal to `status`. Nothing public returns key material.

    `ProviderKeyStore.read` exists -- `status` needs it to compare against the
    running key -- so this asserts the *route* never calls it, which is the
    edge the key would have to cross to reach a browser.
    """
    route = ast.parse(ROUTE.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "read" not in called, (
        "the route calls the store's read(); that is the one method whose "
        "return value is the key itself"
    )


def test_the_fingerprint_is_four_characters_and_a_marker() -> None:
    """Bounded at the source, so no caller has to remember to truncate."""
    service = ast.parse(SERVICE.read_text(encoding="utf-8"))
    fingerprint = next(
        node
        for node in service.body
        if isinstance(node, ast.FunctionDef) and node.name == "fingerprint"
    )
    slices = [
        node
        for node in ast.walk(fingerprint)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
    ]
    assert slices, "fingerprint no longer slices; it must not return a whole key"
    lower = slices[0].slice.lower  # pyright: ignore[reportAttributeAccessIssue]
    assert isinstance(lower, ast.UnaryOp) and isinstance(lower.operand, ast.Constant)
    assert lower.operand.value == 4, "the fingerprint must stay at four characters"


def test_the_key_never_travels_in_a_url() -> None:
    """Bodies are not logged by anything that records request lines; paths are."""
    route = ast.parse(ROUTE.read_text(encoding="utf-8"))
    for node in ast.walk(route):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
            ):
                continue
            path = decorator.args[0] if decorator.args else None
            if isinstance(path, ast.Constant):
                assert "{" not in str(path.value), (
                    f"{node.name} takes a path parameter; a key must arrive in "
                    f"a body, because a path is what a request log records"
                )


def test_the_console_type_and_the_response_model_describe_the_same_payload() -> None:
    """The one seam the two test suites on either side of it cannot reach.

    ``tests/api`` drives the real route against a real store; the console's own
    tests drive the real component against a hand-written payload. Both pass if
    the two payloads have drifted apart -- which is a runtime ``undefined`` in a
    settings panel, and the kind that renders as an empty field rather than an
    error.
    """
    declaration = (
        CONSOLE_TYPES.read_text(encoding="utf-8")
        .split("export interface ProviderKeyView {", 1)[1]
        .split("}", 1)[0]
    )
    declared = {
        line.split(":", 1)[0].strip()
        for line in declaration.splitlines()
        if ":" in line and not line.strip().startswith("//")
    }
    assert declared == ALLOWED_VIEW_FIELDS, (
        f"web/src/api/types.ts and the route's ProviderKeyView disagree: "
        f"only in TypeScript {sorted(declared - ALLOWED_VIEW_FIELDS)}, "
        f"only in Python {sorted(ALLOWED_VIEW_FIELDS - declared)}"
    )
