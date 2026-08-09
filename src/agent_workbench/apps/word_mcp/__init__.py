"""Project-owned, path-free Word document MCP server."""

from agent_workbench.apps.word_mcp.contract import RENDER_DOCUMENT_INPUT_SCHEMA
from agent_workbench.apps.word_mcp.renderer import WORD_DOCUMENT_MEDIA_TYPE
from agent_workbench.apps.word_mcp.server import create_app, create_server

__all__ = [
    "RENDER_DOCUMENT_INPUT_SCHEMA",
    "WORD_DOCUMENT_MEDIA_TYPE",
    "create_app",
    "create_server",
]
