"""Registering a directory as a project, from the directory itself (ADR-074).

The console needs a picker because a browser page cannot learn an absolute path:
``showDirectoryPicker()`` hands back a handle, and the server needs a path. A
terminal has no such problem. The process is *already in* the directory, so the
absolute path is ``Path.cwd()`` and there is nothing to choose.

That is not a shortcut around the picker, it is the same door the console uses
with the choosing step already answered. Both end at one ``PATCH`` carrying an
absolute path, and both are refused by the same sandbox if the path is not one.
Claude Desktop is built the same way -- its folder dialog has a ``providedPath``
branch beside it that skips the prompt and still validates.

The default is the current directory and it is **not** silent: every command
here prints the path it is about to register before it registers it. A tool that
files the wrong directory because somebody ran it one level up should be
correctable by reading its own output, not by inspecting the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO

from agent_workbench.apps.cli.http import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    CliHttpError,
    HttpClientFactory,
    default_http_client,
    identity_headers,
    render_result,
    response_json,
)


def _resolved(raw: str | None) -> Path:
    """The directory this command is about, absolute and resolved.

    ``expanduser`` first: a shell expands ``~`` but a value read from a config
    file or passed through another process has not been through a shell, and the
    server refuses a path that is not absolute -- correctly, but with a message
    about absoluteness that would not explain a literal ``~``.
    """

    return Path(raw or ".").expanduser().resolve()


def run_project_use(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """Point one project at a directory, creating the project if asked to.

    Two requests rather than one, because they are two decisions the server
    owns: which project this is, and which directory it holds. A single
    "register this folder" endpoint would have to invent a project name, and
    the name is the one part of a project a person actually chooses.
    """

    directory = _resolved(getattr(args, "path", None))
    factory = http_client_factory or default_http_client
    headers = identity_headers(args)

    with factory(args.api_url or DEFAULT_API_URL, DEFAULT_TIMEOUT_SECONDS) as client:
        project_id: str | None = getattr(args, "project", None)
        if project_id is None:
            # Named after the directory. This is the Claude Code idiom -- the
            # folder *is* the project, so asking for a name here would be asking
            # a question the answer to which is already on screen.
            name = getattr(args, "name", None) or directory.name
            created = response_json(
                client.post("/v1/projects", json={"name": name}, headers=headers)
            )
            project_id = str(created["project_id"])

        updated = response_json(
            client.patch(
                f"/v1/projects/{project_id}",
                json={"root_path": str(directory)},
                headers=headers,
            )
        )

    render_result(
        {
            "project_id": updated["project_id"],
            "name": updated["name"],
            # Echoed from the *server's* answer, not from what was sent. The
            # server stores the path as given, so these are the same string --
            # and printing the stored one is what makes this output evidence of
            # what happened rather than a restatement of the request.
            "root_path": updated["root_path"],
        },
        as_json=args.json,
        stream=stream,
    )
    return 0


def run_project_list(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """This person's projects and which directory each one holds."""

    factory = http_client_factory or default_http_client
    with factory(args.api_url or DEFAULT_API_URL, DEFAULT_TIMEOUT_SECONDS) as client:
        listed = response_json(
            client.get("/v1/projects", headers=identity_headers(args))
        )

    render_result(
        {
            "projects": [
                {
                    "project_id": project["project_id"],
                    "name": project["name"],
                    # Present and null rather than omitted for the projects that
                    # have no directory: "not registered" and "this build does
                    # not do directories" are different answers.
                    "root_path": project["root_path"],
                }
                for project in listed["projects"]
            ]
        },
        as_json=args.json,
        stream=stream,
    )
    return 0


def run_project_forget(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """Stop pointing a project at its directory. Deletes nothing on disk.

    ``{"root_path": null}``, which is the request that says *no directory* --
    distinct from omitting the field, which says *leave it alone*. The CLI has
    to be able to say the first one or the only way out of a wrong registration
    would be deleting the project.
    """

    factory = http_client_factory or default_http_client
    with factory(args.api_url or DEFAULT_API_URL, DEFAULT_TIMEOUT_SECONDS) as client:
        updated = response_json(
            client.patch(
                f"/v1/projects/{args.project}",
                json={"root_path": None},
                headers=identity_headers(args),
            )
        )

    render_result(
        {"project_id": updated["project_id"], "root_path": updated["root_path"]},
        as_json=args.json,
        stream=stream,
    )
    return 0


_SUBCOMMANDS = {
    "use": run_project_use,
    "list": run_project_list,
    "forget": run_project_forget,
}


def run_project(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> int:
    """Dispatch one `project` subcommand.

    One entry per top-level command, the shape `run_task` established. The
    error translation is not repeated here: `main` wraps every HTTP command in
    the same handler, so a refusal from the server renders identically whichever
    subcommand produced it.
    """

    runner = _SUBCOMMANDS.get(args.project_command)
    if runner is None:
        raise CliHttpError(code="request_failed")
    overrides = (
        {"http_client_factory": http_client_factory}
        if http_client_factory is not None
        else {}
    )
    return runner(args, stream, **overrides)


__all__ = [
    "run_project",
    "run_project_forget",
    "run_project_list",
    "run_project_use",
]
