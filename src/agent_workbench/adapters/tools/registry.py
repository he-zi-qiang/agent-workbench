"""A fixed tool registry.

The set of tools a process will run is decided once, at bootstrap, and does not
change while it runs. That is deliberate: a registry that could gain a tool
mid-run would make "which tools were available" unanswerable for an event log
that has already been written, and would give a compromised request a way to
introduce one.

Removal is a different matter and belongs to live authorization: revoking a
tool takes effect at the next decision, not by mutating this table.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_workbench.domain.tools import ToolSpec
from agent_workbench.ports.tools import ToolBinding


class StaticToolRegistry:
    """Immutable name-to-binding lookup built once at startup."""

    def __init__(self, bindings: Iterable[ToolBinding]) -> None:
        table: dict[str, ToolBinding] = {}
        for binding in bindings:
            name = binding.spec.name
            # Two handlers behind one name would make the effective behaviour
            # depend on registration order.
            if name in table:
                raise ValueError(f"duplicate tool registration: {name}")
            table[name] = binding
        self._bindings = table
        self._specs = tuple(table[name].spec for name in sorted(table))

    def get(self, name: str) -> ToolBinding | None:
        return self._bindings.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Specifications in name order, so a model request is reproducible."""

        return self._specs


__all__ = ["StaticToolRegistry"]
