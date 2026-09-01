"""Every minted identifier's prefix has a name, and that name is declared once.

`domain/identifiers.py` opens by saying the platform's ids "are prefixed and
uniform, so a log line, an event and a database row all say what they point at".
It declared eleven prefixes. Production used three of them, minted six more from
string literals at the call site, and declared a *twelfth* in another layer --
`application/tasks.py`'s `TASK_THREAD_PREFIX = "thr"` against the domain's dead
`WORKFLOW_THREAD_ID_PREFIX = "thread"`. Two declarations of one concept, in two
layers, disagreeing, with the domain's being the one nobody called.

Two of the literals were minted **in two files each**: `new_id("turn")` in both
conversation stores -- the two implementations `tests/contracts` runs the *same*
suite against -- and `new_id("ses")` in two application modules. Neither contract
asserts a prefix, so a divergence between the two stores would have been
invisible in exactly the place this repository claims divergence becomes a
failure.

The rule below is the smallest thing that makes the opening sentence true: a
prefix reaches `new_id` through a name, never as a literal. Where that name
lives is a layering question the rule does not answer -- domain objects declare
theirs in `domain/identifiers.py`, and a process id declares its own beside the
projection that mints it, because a worker is not a domain object.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

#: The one module allowed to hand `new_id` a literal: it is where the names are.
DECLARING_MODULE = PACKAGE_ROOT / "domain" / "identifiers.py"


def _new_id_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "new_id"
    ]


def test_no_identifier_prefix_is_written_as_a_literal() -> None:
    violations: list[str] = []
    for file in sorted(PACKAGE_ROOT.rglob("*.py")):
        if file == DECLARING_MODULE:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for call in _new_id_calls(tree):
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                violations.append(
                    f"{file.relative_to(PROJECT_ROOT)}:{call.lineno}: "
                    f"new_id({first.value!r}) -- give the prefix a name"
                )

    assert not violations, (
        "an identifier prefix is written as a literal at the mint site. Declare "
        "it once -- in `domain/identifiers.py` for a domain object, or beside "
        "the code that mints it for a process id -- and pass the name:\n"
        + "\n".join(violations)
    )


def test_every_declared_prefix_is_actually_minted() -> None:
    """A prefix nobody mints describes an identity space that does not exist.

    `STREAM_ID_PREFIX = "stream"` was the case worth remembering: it was not
    merely idle, it was contradicted. A stream id is either borrowed (chat and
    code use the session id) or minted by whoever owns that stream (`triage`,
    `kgx`) -- so a generic `stream_` said streams have an identity space of
    their own, and they do not.
    """

    declaring = ast.parse(DECLARING_MODULE.read_text(encoding="utf-8"))
    declared = {
        target.id
        for node in declaring.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(target := node.target, ast.Name)
        and target.id.endswith("_PREFIX")
    }

    used: set[str] = set()
    for file in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for call in _new_id_calls(tree):
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Name):
                used.add(first.id)

    unminted = sorted(declared - used)

    assert not unminted, (
        "these prefixes are declared in domain/identifiers.py and nothing mints "
        f"them; delete them rather than describing an id nobody issues: {unminted}"
    )


def test_the_prefixes_stay_distinguishable() -> None:
    """Two ids that look alike are two ids a log line cannot tell apart.

    `ses` is deliberately shared by chat and code sessions -- they are the same
    identity space, and `new_code_session_id` says so by delegating. That is a
    shared *name*, not two names for one value, which is what this checks.
    """

    declaring = ast.parse(DECLARING_MODULE.read_text(encoding="utf-8"))
    by_value: dict[str, list[str]] = {}
    for node in declaring.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(target := node.target, ast.Name)
            and target.id.endswith("_PREFIX")
            and isinstance(value := node.value, ast.Constant)
            and isinstance(value.value, str)
        ):
            by_value.setdefault(value.value, []).append(target.id)

    collisions = {value: names for value, names in by_value.items() if len(names) > 1}

    assert not collisions, (
        "two declared prefixes mint the same string, so the ids they produce "
        f"cannot be told apart: {collisions}"
    )
