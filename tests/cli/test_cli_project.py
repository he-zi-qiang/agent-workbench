"""The terminal's door to registering a project directory (ADR-074).

The console needs a picker because a browser page cannot learn an absolute path.
A shell can: the process is already standing in the directory. What these tests
pin is that the shell door ends at the *same* request the picker's does -- one
`PATCH` carrying an absolute path -- rather than at a second, laxer way in.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agent_workbench.apps.cli.main import main

IDENTITY = ("--tenant-id", "tenant_a", "--principal-id", "user_1")

PROJECT = {
    "project_id": "prj_1",
    "name": "cli-demo",
    "created_at": "2026-08-22T00:00:00Z",
    "updated_at": "2026-08-22T00:00:00Z",
    "archived_at": None,
    "root_path": None,
}


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[str, float], httpx.Client]:
    def factory(base_url: str, timeout_seconds: float) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    return factory


def _run(
    *argv: str, handler: Callable[[httpx.Request], httpx.Response]
) -> tuple[int, str]:
    output = io.StringIO()
    code = main(
        ("project", *argv, "--api-url", "http://api.test", *IDENTITY),
        stream=output,
        http_client_factory=_client_factory(handler),
    )
    return code, output.getvalue()


class _Recorder:
    """Collects every request so a test can assert on what was actually sent."""

    def __init__(self, root_path: str | None = None) -> None:
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self.root_path = root_path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(201, json=PROJECT)
        if request.method == "PATCH":
            return httpx.Response(
                200, json={**PROJECT, "root_path": body.get("root_path")}
            )
        return httpx.Response(200, json={"projects": [PROJECT]})


def test_use_registers_the_current_directory_without_being_told_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the shell door: nothing is chosen and nothing is typed."""

    workdir = tmp_path / "cli-demo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    recorder = _Recorder()

    code, output = _run("use", handler=recorder)

    assert code == 0
    methods = [(method, path) for method, path, _ in recorder.requests]
    # Two requests, because they are two decisions the server owns: which
    # project this is, and which directory it holds.
    assert methods == [("POST", "/v1/projects"), ("PATCH", "/v1/projects/prj_1")]
    # Named after the directory -- the Claude Code idiom, where the folder *is*
    # the project.
    assert recorder.requests[0][2] == {"name": "cli-demo"}
    # Absolute and resolved. `tmp_path` on macOS is under a symlinked `/tmp`, so
    # comparing against the unresolved path would fail for a reason that has
    # nothing to do with this command.
    assert recorder.requests[1][2] == {"root_path": str(workdir.resolve())}
    assert str(workdir.resolve()) in output


def test_an_explicit_path_is_resolved_before_it_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path)
    recorder = _Recorder()

    code, _ = _run("use", "elsewhere", handler=recorder)

    assert code == 0
    # Resolved here, not sent relative and resolved there. The server refuses a
    # relative path -- correctly -- and it would be refusing something the person
    # never meant to send, because in a shell "elsewhere" is unambiguous.
    assert recorder.requests[1][2] == {
        "root_path": str((tmp_path / "elsewhere").resolve())
    }


def test_a_tilde_is_expanded_rather_than_sent_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recorder = _Recorder()

    code, _ = _run("use", "~", handler=recorder)

    assert code == 0
    # A shell expands `~`, but a value from a config file or another process has
    # not been through one. Left literal it would be refused as "not absolute",
    # which is true and would not explain the tilde.
    assert recorder.requests[1][2] == {"root_path": str(Path.home().resolve())}


def test_an_existing_project_can_be_pointed_at_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recorder = _Recorder()

    code, _ = _run("use", "--project", "prj_existing", handler=recorder)

    assert code == 0
    # No POST: `--project` says which project, so inventing one would be
    # creating a second project nobody asked for every time somebody re-pointed
    # an existing one.
    assert [method for method, _, _ in recorder.requests] == ["PATCH"]
    assert recorder.requests[0][1] == "/v1/projects/prj_existing"


def test_forget_sends_null_rather_than_omitting_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recorder = _Recorder()

    code, _ = _run("forget", "--project", "prj_1", handler=recorder)

    assert code == 0
    # `null` means *no directory*; an absent field means *leave it alone*. The
    # CLI has to be able to say the first, or the only way out of a wrong
    # registration is deleting the project.
    method, path, body = recorder.requests[0]
    assert (method, path) == ("PATCH", "/v1/projects/prj_1")
    assert body == {"root_path": None}
    assert "root_path" in body


def test_list_reports_which_projects_hold_a_directory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "projects": [
                    {**PROJECT, "root_path": "/srv/alpha"},
                    {**PROJECT, "project_id": "prj_2", "name": "无目录"},
                ]
            },
        )

    code, output = _run("list", "--json", handler=handler)

    assert code == 0
    listed = json.loads(output)["projects"]
    # Present-and-null, not omitted: "no directory registered" and "this build
    # does not do directories" are different answers, and a caller scripting
    # against this needs to tell them apart.
    assert [project["root_path"] for project in listed] == ["/srv/alpha", None]
