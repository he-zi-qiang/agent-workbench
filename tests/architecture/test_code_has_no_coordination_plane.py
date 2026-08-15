"""Code declined a coordination plane, and declining it has to stay declined.

Everything Code is -- one process, one in-memory slot per session, a turn that
dies with the process -- rests on it not having a lease, a reaper, a checkpoint
or a graph. Those are not missing features. Each one is a thing that would have
to be able to *release* something after a crash, and the price of having one is
having all of them.

Which makes this the kind of decision that erodes rather than gets reversed.
Nobody would propose giving Code a task registry; somebody would import one
helper from it because that helper had the right shape, and the next reader
would find a module that half-participates in a plane it does not belong to.

Two scans, and the second is the one the plan did not ask for. Imports are the
obvious way in. The other way is calling the chat-turn ledger through the
conversation store, which Code already holds a reference to -- ``claim_turn``
is right there on the same object, and using it would give Code a lease that
nothing in this design can ever return.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

#: The modules that make up Code, as globs so a file added tomorrow is scanned
#: without anybody remembering to list it.
CODE_MODULE_GLOBS = (
    "application/code_*.py",
    "application/session_workspace.py",
    "apps/api/routes/code.py",
)

#: Import prefixes that would mean Code had joined the coordination plane.
FORBIDDEN_IMPORTS = (
    "agent_workbench.ports.task_registry",
    "agent_workbench.adapters.persistence.task_registry",
    "agent_workbench.ports.execution_guard",
    "agent_workbench.adapters.persistence.execution_guard",
    "agent_workbench.ports.approvals",
    "agent_workbench.application.approvals",
    "agent_workbench.adapters.langgraph",
    "agent_workbench.workflows",
)

#: The chat-turn ledger, reachable on an object Code legitimately holds. A code
#: session that claimed a turn would take a lease only the expiry reaper can
#: return, and this design has no reaper.
FORBIDDEN_CALLS = (
    "claim_turn",
    "prepare_release",
    "mark_released",
    "finish_failed",
    "finish_running_if_current",
    "list_release_pending",
)


def _code_modules() -> tuple[Path, ...]:
    found: list[Path] = []
    for pattern in CODE_MODULE_GLOBS:
        found.extend(sorted(PACKAGE_ROOT.glob(pattern)))
    return tuple(found)


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _called_attributes(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_no_code_module_imports_the_coordination_plane() -> None:
    offences: list[str] = []
    for module in _code_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for imported in sorted(_imported_names(tree)):
            if imported.startswith(FORBIDDEN_IMPORTS):
                offences.append(f"{module.name} imports {imported}")

    assert offences == []


def test_no_code_module_reaches_the_chat_turn_ledger() -> None:
    """The near miss: Code holds the store those methods live on.

    An import scan alone would pass while a code module called ``claim_turn``
    on the conversation store it was handed, which is the one way this mistake
    is actually likely to be made.
    """

    offences: list[str] = []
    for module in _code_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for called in sorted(_called_attributes(tree) & set(FORBIDDEN_CALLS)):
            offences.append(f"{module.name} calls {called}()")

    assert offences == []


def test_the_scan_covers_the_modules_it_claims_to() -> None:
    """The control. A glob that matched nothing would agree with everything.

    Named rather than counted: a rename that emptied one glob would leave the
    others matching and the total still non-zero.
    """

    scanned = {module.name for module in _code_modules()}

    assert "code_session.py" in scanned
    assert "code_approvals.py" in scanned
    assert "session_workspace.py" in scanned
