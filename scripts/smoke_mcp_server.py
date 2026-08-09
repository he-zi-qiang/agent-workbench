#!/usr/bin/env python3
"""Probe one loopback MCP process without invoking any of its tools.

This is deliberately a protocol smoke, not a Task smoke. It proves that the
process is healthy and that the official MCP client can discover the tools the
deployment expects. A real Task additionally needs a model provider,
PostgreSQL, the Task Worker and a principal carrying ``mcp:<alias>`` -- so a
green run here says the server is reachable and says nothing about a Task.

One script for every project-owned server, rather than a copy per server. The
second server was the reason to generalise it: a copy is a second place for the
cursor-loop and the health wait to be wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

from agent_workbench.adapters.mcp.client import (
    RemoteToolDefinition,
    connect_mcp_client,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        default="MCP",
        help="What to call this server in messages, e.g. `word` or `web`.",
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="Streamable HTTP MCP endpoint.",
    )
    parser.add_argument(
        "--health-url",
        required=True,
        help="Loopback HTTP health endpoint.",
    )
    parser.add_argument(
        "--expect-tool",
        action="append",
        default=[],
        help="Remote tool name that must be advertised. Repeatable.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=10.0,
        help="How long to wait for the health endpoint during startup.",
    )
    return parser


async def _wait_for_health(url: str, *, label: str, wait_seconds: float) -> int:
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.status_code
            except httpx.HTTPError as error:
                last_error = error
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"{label} MCP health probe did not succeed"
                    ) from error
                await asyncio.sleep(0.2)
    raise RuntimeError("unreachable health probe state") from last_error


async def _list_tools(endpoint: str, *, label: str) -> tuple[RemoteToolDefinition, ...]:
    tools: list[RemoteToolDefinition] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    async with connect_mcp_client(endpoint, timeout_seconds=10) as client:
        while True:
            page = await client.list_tools_page(cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise RuntimeError(f"{label} MCP tools/list repeated a cursor")
            seen_cursors.add(cursor)
    return tuple(tools)


async def _run(args: argparse.Namespace) -> None:
    status = await _wait_for_health(
        args.health_url, label=args.label, wait_seconds=args.wait_seconds
    )
    tools = await _list_tools(args.endpoint, label=args.label)
    names = {tool.name for tool in tools}
    missing = sorted(set(args.expect_tool) - names)
    if missing:
        raise RuntimeError(
            f"{args.label} MCP is missing expected tools: " + ", ".join(missing)
        )

    print(f"health  {status} {args.health_url}")
    print(f"mcp     {args.endpoint}")
    print("tools")
    for tool in sorted(tools, key=lambda item: item.name):
        description = (tool.description or "").strip() or "(no description)"
        print(f"  {tool.name}: {description}")


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args))
    except (RuntimeError, httpx.HTTPError) as error:
        print(f"{args.label} MCP check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
