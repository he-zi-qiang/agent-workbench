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
from collections.abc import Callable, Sequence
from typing import TextIO

import httpx

from agent_workbench.apps.cli.commands import (
    run_approval,
    run_artifact,
    run_chat,
    run_repl,
    run_search,
)
from agent_workbench.apps.cli.demo import (
    DEMO_PROMPT,
    DEMO_REPLY,
    DEMO_TOOL_NAMES,
    build_demo,
    execute,
)
from agent_workbench.apps.cli.http import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    CliHttpError,
    HttpClientFactory,
    render_error,
)
from agent_workbench.apps.cli.rendering import JsonRenderer, Renderer, TextRenderer
from agent_workbench.apps.cli.task import run_task
from agent_workbench.apps.cli.upload import run_upload
from agent_workbench.domain.runs import AgentOutcome

#: Every command that speaks HTTP shares one failure path: a caller-safe error
#: category, the status code, and never a server-provided body.
HTTP_COMMANDS: dict[str, Callable[..., int]] = {
    "task": run_task,
    "search": run_search,
    "chat": run_chat,
    "repl": run_repl,
    "approval": run_approval,
    "artifact": run_artifact,
}

EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_CANCELLED = 130

EXIT_CODES: dict[str, int] = {
    "completed": EXIT_COMPLETED,
    "failed": EXIT_FAILED,
    "cancelled": EXIT_CANCELLED,
}


def _add_endpoint(parser: argparse.ArgumentParser) -> None:
    """Where to send the request, and who to send it as.

    Applied to every HTTP command from one place. A command that grew its own
    copy of these would eventually differ from the rest by one flag, and the
    one it was missing would be ``--scope``.
    """

    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--tenant-id", required=True, help="Value for x-tenant-id.")
    parser.add_argument(
        "--principal-id", required=True, help="Value for x-principal-id."
    )
    parser.add_argument(
        "--scope",
        action="append",
        help=(
            "Permission scope this caller holds. Repeatable. A tool that "
            "declares a scope is refused without it: exporting a report needs "
            "'artifact:export'."
        ),
    )


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
    _add_endpoint(task)
    task_commands = task.add_subparsers(dest="task_command", required=True)

    task_list = task_commands.add_parser("list", help="List your own Tasks.")
    task_list.add_argument(
        "--status",
        action="append",
        help="Only Tasks in this status. Repeatable; omitted means every status.",
    )
    task_list.add_argument("--limit", type=int, default=50)
    task_list.add_argument("--cursor", help="Resume after this opaque cursor.")
    task_list.add_argument("--json", action="store_true", help="Emit one JSON object.")

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

    upload = subcommands.add_parser(
        "upload",
        help="Transfer a file and complete it into a document version.",
        description=(
            "Declares, transfers and completes through /v1/uploads, the same "
            "three calls any other client makes. The ingestion worker picks "
            "the version up from the outbox and indexes it."
        ),
    )
    upload.add_argument("path", help="File to upload.")
    _add_endpoint(upload)
    upload.add_argument("--document-id", required=True)
    upload.add_argument("--knowledge-base-id", required=True)
    upload.add_argument(
        "--grant",
        action="append",
        help=(
            "Principal allowed to read this document. Repeatable. The owner "
            "is not implied: an unshared document is readable by nobody else."
        ),
    )
    upload.add_argument(
        "--media-type", help="Override the type guessed from the filename."
    )
    upload.add_argument("--json", action="store_true", help="Emit one JSON object.")

    search = subcommands.add_parser(
        "search",
        help="Retrieve passages without asking a model to talk about them.",
        description=(
            "Returns the retrieval packet a fixed chat turn would have put in "
            "front of the model. This is the half of chat that needs no "
            "provider, so a deployment with no model key can still show what "
            "its corpus holds -- and a retrieval problem can be told apart "
            "from a generation one."
        ),
    )
    _add_endpoint(search)
    search.add_argument("--query", required=True)
    search.add_argument("--knowledge-base-id", required=True)
    search.add_argument("--top-k", type=int, default=8)
    search.add_argument("--json", action="store_true", help="Emit one JSON object.")

    repl = subcommands.add_parser(
        "repl",
        help="Keep a session open and watch each turn as it runs.",
        description=(
            "Reads questions from stdin and prints what the run is doing while "
            "it does it, folded to one line per stage -- the same stages the "
            "web console shows. `/task <objective>` runs a durable Task in the "
            "same loop, including answering its approval; `/steps` opens the "
            "last turn and shows every event behind those lines."
        ),
    )
    _add_endpoint(repl)
    repl.add_argument(
        "--knowledge-base-id",
        default=None,
        help="Answer from this corpus. Omit for free conversation; /kb switches.",
    )
    repl.add_argument(
        "--no-color",
        action="store_true",
        help="Write no escape sequences, even to a terminal.",
    )
    repl.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    chat = subcommands.add_parser(
        "chat",
        help="Ask a question and get an answer that names its sources.",
        description=(
            "Served only where the API has a model provider. Without one the "
            "route is not registered at all, so this reports not_found rather "
            "than a failure that looks like the model's."
        ),
    )
    _add_endpoint(chat)
    chat_commands = chat.add_subparsers(dest="chat_command", required=True)

    ask = chat_commands.add_parser("ask", help="Ask one question.")
    ask.add_argument("--question", required=True)
    ask.add_argument("--knowledge-base-id", required=True)
    ask.add_argument(
        "--session-id",
        help=(
            "Continue this conversation. Omitted, the CLI opens one and prints "
            "its id, so a follow-up question can be part of the same thread."
        ),
    )
    ask.add_argument("--title", help="Title for a session opened by this command.")
    ask.add_argument("--top-k", type=int, default=8)
    ask.add_argument(
        "--idempotency-key",
        default=None,
        help=(
            "Retry key sent as Idempotency-Key. A repeated key returns the "
            "answer the first attempt produced rather than asking again."
        ),
    )
    ask.add_argument("--json", action="store_true", help="Emit one JSON object.")

    history = chat_commands.add_parser("history", help="Replay one session.")
    history.add_argument("--session-id", required=True)
    history.add_argument("--json", action="store_true", help="Emit one JSON object.")

    approval = subcommands.add_parser(
        "approval",
        help="Find, read and answer the approvals waiting on you.",
        description=(
            "A Task that stops for a person stays stopped until somebody "
            "answers. Until this command existed the only way to answer was "
            "to read the Task timeline for an id and then hand-write the HTTP "
            "request."
        ),
    )
    _add_endpoint(approval)
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)

    approval_list = approval_commands.add_parser(
        "list", help="List your own approvals."
    )
    approval_list.add_argument(
        "--status",
        choices=("pending", "approved", "rejected"),
        help="Only approvals in this state. '--status pending' is the queue.",
    )
    approval_list.add_argument("--limit", type=int, default=50)
    approval_list.add_argument("--cursor", help="Resume after this opaque cursor.")
    approval_list.add_argument(
        "--json", action="store_true", help="Emit one JSON object."
    )

    approval_get = approval_commands.add_parser("get", help="Read one approval.")
    approval_get.add_argument("approval_id")
    approval_get.add_argument(
        "--json", action="store_true", help="Emit one JSON object."
    )

    for decision in ("approved", "rejected"):
        decide = approval_commands.add_parser(
            decision,
            help=f"Record a {decision} decision and let the Task continue.",
        )
        decide.add_argument("approval_id")
        decide.add_argument(
            "--decision-version",
            type=int,
            default=1,
            help=(
                "Idempotency for the decision. The same version twice records "
                "one answer and requeues once; a higher one supersedes."
            ),
        )
        decide.add_argument("--json", action="store_true", help="Emit one JSON object.")

    artifact = subcommands.add_parser(
        "artifact",
        help="Read one artifact back.",
        description=(
            "The exported report, an evidence bundle or an agent outcome. "
            "Authorized as the principal that stored it: an id is not a "
            "capability."
        ),
    )
    _add_endpoint(artifact)
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_get = artifact_commands.add_parser("get", help="Download one artifact.")
    artifact_get.add_argument("artifact_id")
    artifact_get.add_argument(
        "--output", help="Write the bytes here instead of to standard output."
    )
    artifact_get.add_argument(
        "--json", action="store_true", help="Emit one JSON object."
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


def main(
    argv: Sequence[str] | None = None,
    stream: TextIO | None = None,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """Parse arguments, run the requested command and return its exit code."""

    args = build_parser().parse_args(argv)
    output = stream if stream is not None else sys.stdout
    if args.command == "upload":
        return run_upload(
            args,
            output,
            **(
                {"http_client_factory": http_client_factory}
                if http_client_factory is not None
                else {}
            ),
        )
    runner = HTTP_COMMANDS.get(args.command)
    if runner is not None:
        overrides = (
            {"http_client_factory": http_client_factory}
            if http_client_factory is not None
            else {}
        )
        try:
            return runner(args, output, **overrides)
        except httpx.HTTPError:
            # A transport failure is not a server answer, so it gets its own
            # category rather than being reported as a request that failed.
            return render_error(
                CliHttpError(code="transport_error"), output, as_json=args.json
            )
        except CliHttpError as error:
            return render_error(error, output, as_json=args.json)
    outcome = run_demo(args, output)
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
