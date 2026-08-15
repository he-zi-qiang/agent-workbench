"""Nobody outside this system may say which version of a workspace to use.

A workspace version is an artifact id, and the artifact store's own contract
says the quiet part out loud: "hard to guess is not an authorization rule".
Reads and writes are scoped to a tenant and a principal and nothing narrower,
so a principal who could name a version could name one belonging to another of
their own sessions -- and read or overwrite a working set that session is in
the middle of.

Today that is unreachable, because no entrance accepts one. The version reaches
the tools through a ``ContextVar`` the service enters, and reaches the database
through a compare-and-set the tools never see. This test is what keeps the
entrance closed: adding a `workspace_version` query parameter to a route, or a
`manifest_id` property to a tool schema, both look perfectly reasonable in
isolation and both open it.

Scoped to the two surfaces where input arrives from outside the process: HTTP
route signatures, and the JSON schemas a model fills in.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

#: Names that would let a caller choose a working set. ``version`` alone is not
#: here on purpose -- it is an ordinary word, and a document revision or a
#: schema version has every right to it.
FORBIDDEN_INPUT_NAMES = frozenset(
    {"workspace_version", "manifest_id", "workspace_manifest"}
)


def _route_modules() -> tuple[Path, ...]:
    return tuple(sorted((PACKAGE_ROOT / "apps" / "api" / "routes").glob("*.py")))


def _tool_modules() -> tuple[Path, ...]:
    return tuple(sorted((PACKAGE_ROOT / "adapters" / "tools").glob("*.py")))


def _route_parameter_names(tree: ast.AST) -> set[str]:
    """Every parameter of every module-level async function.

    Routes are the async functions in these modules; a helper caught alongside
    them is a false positive nobody will mind, since none of these names has an
    innocent use here either.
    """

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            names.add(argument.arg)
    return names


def _model_field_names(tree: ast.AST) -> set[str]:
    """Annotated class attributes, i.e. the fields of a request body model."""

    return {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _schema_property_names(tree: ast.AST) -> set[str]:
    """Every string key under a ``properties`` mapping in a tool schema."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "properties"):
                continue
            if not isinstance(value, ast.Dict):
                continue
            names.update(
                entry.value
                for entry in value.keys
                if isinstance(entry, ast.Constant) and isinstance(entry.value, str)
            )
    return names


def test_no_route_accepts_a_workspace_version() -> None:
    offences: list[str] = []
    for module in _route_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        found = (_route_parameter_names(tree) | _model_field_names(tree)) & (
            FORBIDDEN_INPUT_NAMES
        )
        offences.extend(f"{module.name} accepts {name}" for name in sorted(found))

    assert offences == []


def test_no_tool_schema_offers_a_workspace_version() -> None:
    offences: list[str] = []
    for module in _tool_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        found = _schema_property_names(tree) & FORBIDDEN_INPUT_NAMES
        offences.extend(f"{module.name} offers {name}" for name in sorted(found))

    assert offences == []


def test_both_scans_are_looking_at_something() -> None:
    """The control, and it checks the *parsers* rather than the file lists.

    A glob that still matched files while the extractor had stopped finding
    parameters would pass both tests above for the wrong reason. So this
    asserts each extractor recovers a name that is definitely there.
    """

    routes = {module.name: module for module in _route_modules()}
    tools = {module.name: module for module in _tool_modules()}

    chat = ast.parse(routes["chat.py"].read_text(encoding="utf-8"))
    workspace = ast.parse(tools["workspace.py"].read_text(encoding="utf-8"))

    assert "session_id" in _route_parameter_names(chat)
    assert "name" in _schema_property_names(workspace)
