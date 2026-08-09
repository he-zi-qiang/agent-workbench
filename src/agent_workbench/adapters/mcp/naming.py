"""Deterministic names for tools discovered through MCP (ADR-025)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from agent_workbench.domain.tools import ToolName

_TOOL_NAME: TypeAdapter[str] = TypeAdapter(ToolName)
_SEPARATORS = frozenset({"-", ".", " "})


@dataclass(frozen=True, slots=True)
class SkipReason:
    """Why one remote tool was not admitted, in operator-facing words."""

    remote_name: str
    reason: str


def tool_name_for(alias: str, remote_name: str) -> ToolName | SkipReason:
    """Return the stable ``mcp_<alias>_<remote>`` name, or a refusal.

    Only the mappings ADR-025 explicitly chose are performed.  In particular,
    unsupported characters are not silently deleted: deletion would collapse
    distinct remote names into one local name and make a later collision
    impossible to explain.
    """

    if not remote_name:
        return SkipReason(remote_name=remote_name, reason="remote tool name is empty")

    normalized: list[str] = []
    for character in remote_name:
        if "A" <= character <= "Z":
            normalized.append(character.lower())
        elif "a" <= character <= "z" or "0" <= character <= "9" or character == "_":
            normalized.append(character)
        elif character in _SEPARATORS:
            normalized.append("_")
        else:
            return SkipReason(
                remote_name=remote_name,
                reason=(
                    "remote tool name contains an unsupported character "
                    f"U+{ord(character):04X}"
                ),
            )

    candidate = f"mcp_{alias}_{''.join(normalized)}"
    try:
        return _TOOL_NAME.validate_python(candidate)
    except ValidationError:
        return SkipReason(
            remote_name=remote_name,
            reason=(
                "normalized tool name does not satisfy the local 64-character contract"
            ),
        )


__all__ = ["SkipReason", "tool_name_for"]
