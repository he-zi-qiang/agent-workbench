"""An error code is a contract with every reader of every stream.

``ErrorCode`` is a closed ``Literal``. A code invented at a call site is not a
narrower promise, it is a value that type-checks only because the literal was
written where nothing compares it to the set -- and it reaches a client, a CLI
renderer and a tracing backend as if it had always been a member.

The temptation is specific and recurring: a new refusal shows up, and the
nearest true sentence is a new code for it. Sometimes that is right, and then
the ``Literal`` moves and the change is visible. What this test stops is the
other case, where the code is added at the call site and nowhere else.

Scoped to the modules where refusals are constructed rather than to the whole
package, because that is where the pressure is. The second assertion is what
keeps the first honest: a scan that matched nothing would pass forever.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from agent_workbench.domain.errors import ErrorCode

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

#: Where a refusal is turned into an ``ErrorInfo`` a caller will see. The
#: gateway is the one that matters most: every tool call passes through it, and
#: every way of not running one ends as a code.
REFUSING_MODULES = (
    Path("runtime/tool_gateway.py"),
    Path("runtime/tool_executor.py"),
    Path("runtime/agent_runtime.py"),
    Path("application/chat.py"),
    # The model adapter joined this list with ADR-0084, which gave it a second
    # code to choose between rather than one to always write. A call site that
    # picks is a call site that can pick a word nobody declared, and this is
    # the only such place outside `core`.
    Path("adapters/models/deepseek.py"),
)


def _declared_codes(module: Path) -> frozenset[str]:
    """Every literal passed as ``ErrorInfo(code=...)`` in one module."""

    tree = ast.parse((PACKAGE_ROOT / module).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else None
        if isinstance(function, ast.Name):
            name = function.id
        if name != "ErrorInfo":
            continue
        for keyword in node.keywords:
            if keyword.arg != "code":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                found.add(keyword.value.value)
    return frozenset(found)


@pytest.mark.parametrize("module", REFUSING_MODULES, ids=lambda path: str(path))
def test_every_error_code_written_at_a_call_site_is_a_declared_one(
    module: Path,
) -> None:
    undeclared = _declared_codes(module) - frozenset(get_args(ErrorCode))

    assert undeclared == frozenset()


def test_the_scan_finds_codes_at_all() -> None:
    """The control. A scanner that matched nothing would agree with everything.

    Pinned to the gateway rather than to a total across modules, so that a
    refactor moving refusals out of one file cannot quietly leave this test
    passing on somebody else's codes.
    """

    assert _declared_codes(Path("runtime/tool_gateway.py"))
