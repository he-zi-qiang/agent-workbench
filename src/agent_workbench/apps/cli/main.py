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
from agent_workbench.apps.cli.task import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    HttpClientFactory,
    TaskCliError,
    render_error,
    run_task,
)
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
    task = subcommands.add_parser(
        "task",
        help="Control durable Tasks through the HTTP API.",
        description=(
            "Use the API's development identity headers. In production, use "
            "the deployment's authenticated client instead."
        ),
    )
    task.add_argument("--api-url", default=DEFAULT_API_URL)
    task.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    task.add_argument("--tenant-id", required=True, help="Value for x-tenant-id.")
    task.add_argument("--principal-id", required=True, help="Value for x-principal-id.")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    submit = task_commands.add_parser("submit", help="Submit one durable Task.")
    submit.add_argument("--objective", required=True)
    submit.add_argument("--max-revisions", type=int, default=2)
    submit.add_argument("--knowledge-base-id")
    submit.add_argument(
        "--idempotency-key",
        help=(
            "Retry key sent as Idempotency-Key. If omitted, the CLI generates "
            "one and prints it in the result."
        ),
    )
    submit.add_argument("--json", action="store_true", help="Emit one JSON object.")

    get = task_commands.add_parser("get", help="Read one of your Tasks.")
    get.add_argument("task_id")
    get.add_argument("--json", action="store_true", help="Emit one JSON object.")

    timeline = task_commands.add_parser("timeline", help="Read a Task event page.")
    timeline.add_argument("task_id")
    timeline.add_argument("--cursor", help="Resume after this opaque cursor.")
    timeline.add_argument("--limit", type=int, default=200)
    timeline.add_argument("--json", action="store_true", help="Emit one JSON object.")

    cancel = task_commands.add_parser("cancel", help="Cancel one of your Tasks.")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--json", action="store_true", help="Emit one JSON object.")
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


def main(
    argv: Sequence[str] | None = None,
    stream: TextIO | None = None,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """Parse arguments, run the requested command and return its exit code."""

    args = build_parser().parse_args(argv)
    output = stream if stream is not None else sys.stdout
    if args.command == "task":
        try:
            return run_task(
                args,
                output,
                **(
                    {"http_client_factory": http_client_factory}
                    if http_client_factory is not None
                    else {}
                ),
            )
        except TaskCliError as error:
            return render_error(error, output, as_json=args.json)
    outcome = run_demo(args, output)
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
