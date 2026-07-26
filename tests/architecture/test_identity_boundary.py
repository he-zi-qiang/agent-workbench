"""ADR-012's rule, enforced rather than remembered.

Identity is an interface-layer result. A ``PrincipalContext`` built anywhere
else is a component deciding for itself who is calling, which is the same
mistake as reading the owner out of a request body -- and it is the kind of
mistake that reviews miss, because the line that does it looks reasonable on
its own.

The allowlist is the point. Adding a module to it is a visible change somebody
has to justify; without one, the rule survives exactly as long as everybody
remembers it.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

PRINCIPAL_TYPE = "PrincipalContext"

# Every module permitted to decide who is calling. Both are process entry
# points: one resolves a real request, the other scripts a demonstration.
IDENTITY_BOUNDARY_MODULES = frozenset(
    {
        "agent_workbench.apps.api.identity",
        "agent_workbench.apps.cli.demo",
        # The definition itself.
        "agent_workbench.domain.policies",
    }
)


def _product_python_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            file
            for file in PACKAGE_ROOT.rglob("*.py")
            if "__pycache__" not in file.parts
        )
    )


def _module_name(file: Path) -> str:
    relative = file.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _constructs_principal(file: Path) -> bool:
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == PRINCIPAL_TYPE:
            return True
        if isinstance(target, ast.Attribute) and target.attr == PRINCIPAL_TYPE:
            return True
    return False


def test_the_allowlisted_modules_exist() -> None:
    """A stale allowlist would silently permit nothing and prove nothing."""

    modules = {_module_name(file) for file in _product_python_files()}

    assert modules >= IDENTITY_BOUNDARY_MODULES


def test_only_the_identity_boundary_builds_a_principal() -> None:
    builders = {
        _module_name(file)
        for file in _product_python_files()
        if _constructs_principal(file)
    }

    assert builders, (
        "the guard must observe the existing construction sites; "
        "otherwise source discovery or AST extraction has regressed"
    )

    violations = sorted(builders - IDENTITY_BOUNDARY_MODULES)

    assert not violations, (
        "identity is resolved at the interface edge and handed down "
        "(ADR-012). These modules build a PrincipalContext of their own:\n"
        + "\n".join(f"  {module}" for module in violations)
    )


def test_the_api_refuses_a_remote_deployment_scope() -> None:
    """The refusal is what makes deferring a real identity provider safe.

    ADR-012 rests on it: relax this and the decision it records lapses with it.

    This covers the scope label and nothing else. It once read as though it
    guarded the whole boundary, and for a while the committed default was
    ``0.0.0.0`` -- a ``local`` process listening on every interface, past a
    green test. The bind address is a separate property with its own tests in
    ``tests/api/test_bind_address.py``, which open a socket rather than read
    a label.
    """

    source = (PACKAGE_ROOT / "apps" / "api" / "dependencies.py").read_text(
        encoding="utf-8"
    )

    assert 'deployment_scope == "remote"' in source
    assert "InsecureDeploymentError" in source
