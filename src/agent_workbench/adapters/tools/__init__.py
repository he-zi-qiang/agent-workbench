"""Tool registry and the tools that ship with the walking skeleton."""

from agent_workbench.adapters.tools.fakes import (
    READ_DOCUMENT_SPEC,
    TEXT_STATISTICS_SPEC,
    read_document_tool,
    text_statistics_tool,
)
from agent_workbench.adapters.tools.registry import StaticToolRegistry

__all__ = [
    "READ_DOCUMENT_SPEC",
    "TEXT_STATISTICS_SPEC",
    "StaticToolRegistry",
    "read_document_tool",
    "text_statistics_tool",
]
