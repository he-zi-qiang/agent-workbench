"""Measure the incremental startup slice owned by one MCP server.

This is intentionally not a provider benchmark.  It uses the official SDK's
in-memory transport so the number describes this repository's server/discover,
directory translation and ToolBinding construction cost without folding a
particular network into the result.  A deployment should add its own endpoint
latency to this lower-bound measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from mcp.server import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_workbench.adapters.mcp.client import connect_mcp_client
from agent_workbench.adapters.mcp.registry_source import discover_bindings
from agent_workbench.adapters.memory import InMemoryArtifactStore


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _server(tool_count: int) -> MCPServer[Any]:
    server = MCPServer("agent-workbench-startup-benchmark")

    def tool_for(index: int) -> Any:
        async def echo(value: str = "") -> str:
            """Return a deterministic value for the transport fixture."""

            return f"{index}:{value}"

        return echo

    for index in range(tool_count):
        server.add_tool(tool_for(index), name=f"tool_{index:03d}")
    return server


async def _sample(server: MCPServer[Any], tool_count: int) -> float:
    allowed = tuple(f"tool_{index:03d}" for index in range(tool_count))
    artifacts = InMemoryArtifactStore()
    started = time.perf_counter_ns()
    async with connect_mcp_client(cast(Any, server), timeout_seconds=30) as client:
        bindings = await discover_bindings(
            alias="benchmark",
            allowed_remote_tools=allowed,
            timeout_seconds=30,
            client=client,
            artifacts=artifacts,
            artifact_threshold_bytes=65_536,
            max_result_bytes=10_485_760,
            max_artifact_bytes=104_857_600,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if len(bindings) != tool_count:
        raise RuntimeError(
            f"benchmark discovered {len(bindings)} of {tool_count} configured tools"
        )
    return elapsed_ms


async def _run(*, tool_count: int, warmups: int, rounds: int) -> dict[str, object]:
    server = _server(tool_count)
    for _ in range(warmups):
        await _sample(server, tool_count)
    samples = [await _sample(server, tool_count) for _ in range(rounds)]
    ordered = sorted(samples)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "benchmark": "mcp_incremental_worker_startup",
        "transport": "official_sdk_in_memory",
        "python": platform.python_version(),
        "mcp_sdk": version("mcp"),
        "tools": tool_count,
        "warmups": warmups,
        "rounds": rounds,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "scope": "SDK server/discover + tools/list + local ToolBinding construction",
        "network_latency_included": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=_positive, default=20)
    parser.add_argument("--rounds", type=_positive, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.warmups < 0:
        parser.error("--warmups cannot be negative")
    report = asyncio.run(
        _run(
            tool_count=arguments.tools,
            warmups=arguments.warmups,
            rounds=arguments.rounds,
        )
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
