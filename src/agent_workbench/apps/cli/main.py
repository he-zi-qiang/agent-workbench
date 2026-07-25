"""The command line entry point.

The CLI is a debugging, demonstration and evaluation surface. It calls the same
ports the API will call and renders the same events an SSE client will receive;
it does not reach past them into an executor's internals, and it does not
acquire a second way to run a tool.

Exit codes follow the run's own terminal status, so a script can branch on the
outcome without parsing the transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from agent_workbench.apps.cli.demo import (
    DEMO_PROMPT,
    DEMO_REPLY,
    DEMO_TOOL_NAMES,
    build_demo,
    execute,
)
from agent_workbench.apps.cli.rendering import JsonRenderer, Renderer, TextRenderer
from agent_workbench.domain.runs import AgentOutcome

EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_CANCELLED = 130

EXIT_CODES: dict[str, int] = {
    "completed": EXIT_COMPLETED,
    "failed": EXIT_FAILED,
    "cancelled": EXIT_CANCELLED,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-cli",
        description=(
            "Local entry point for Agent Workbench. Runs offline against "
            "deterministic adapters; it never contacts a model provider, a "
            "database or a vector store."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    demo = subcommands.add_parser(
        "demo",
        help="Run one scripted agent turn and print its unified events.",
        description=(
            "Drive the scripted model through the single-turn executor and "
            "render the run. Output is byte identical on every run."
        ),
    )
    demo.add_argument(
        "--prompt",
        default=DEMO_PROMPT,
        help="The user turn to send.",
    )
    demo.add_argument(
        "--reply",
        default=DEMO_REPLY,
        help="The assistant turn the scripted model replays.",
    )
    demo.add_argument(
        "--tool",
        choices=(*DEMO_TOOL_NAMES, "none"),
        default="read_document",
        help=(
            "Which tool the scripted model calls before answering. "
            "'none' runs a single text-only turn."
        ),
    )
    demo.add_argument(
        "--deny",
        action="store_true",
        help=(
            "Submit an envelope that permits no tool. The handler never runs "
            "and the model still receives an answer for its call."
        ),
    )
    demo.add_argument(
        "--max-steps",
        type=int,
        default=4,
        help="Step ceiling for the run.",
    )
    demo.add_argument(
        "--max-tool-calls",
        type=int,
        default=8,
        help="Tool call ceiling for the run.",
    )
    demo.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="text streams the answer and replays the timeline; json emits JSONL.",
    )
    return parser


def _renderer(output_format: str, stream: TextIO) -> Renderer:
    if output_format == "json":
        return JsonRenderer(stream=stream)
    return TextRenderer(stream=stream)


def run_demo(args: argparse.Namespace, stream: TextIO) -> AgentOutcome:
    demo = build_demo(
        prompt=args.prompt,
        reply=args.reply,
        tool=None if args.tool == "none" else args.tool,
        deny_tools=args.deny,
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
    )
    return asyncio.run(
        execute(demo, _renderer(args.format, stream), prompt=args.prompt)
    )


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    """Parse arguments, run the requested command and return its exit code."""

    args = build_parser().parse_args(argv)
    outcome = run_demo(args, stream if stream is not None else sys.stdout)
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
