"""Project-owned MCP server that reads the web and never writes to it."""

from agent_workbench.apps.web_mcp.contract import (
    DOWNLOAD_DOCUMENT_INPUT_SCHEMA,
    FETCH_PAGE_INPUT_SCHEMA,
)
from agent_workbench.apps.web_mcp.fetcher import WebFetcher, WebFetchError
from agent_workbench.apps.web_mcp.server import create_app, create_server

__all__ = [
    "DOWNLOAD_DOCUMENT_INPUT_SCHEMA",
    "FETCH_PAGE_INPUT_SCHEMA",
    "WebFetchError",
    "WebFetcher",
    "create_app",
    "create_server",
]
