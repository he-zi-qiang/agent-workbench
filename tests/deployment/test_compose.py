"""Static validation of the checked-in local Compose topology."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"


def test_compose_configuration_is_renderable_when_docker_is_available() -> None:
    """This parses the real Compose file without building or starting services."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed")
    result = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _compose() -> dict[str, object]:
    """The rendered topology, parsed. Skips when Docker is absent.

    Rendered rather than read: the file uses anchors and merge keys, so reading
    the YAML directly would assert what was written instead of what Compose
    actually produces.
    """

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed")
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "demo",
            "config",
            # JSON rather than the default YAML: PyYAML is only present here
            # transitively, and a test that depends on a package nothing
            # declares breaks the day a resolver drops it.
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_the_demo_topology_runs_two_task_workers() -> None:
    """Claim, lease, epoch and fencing only mean anything under contention.

    With one Worker every one of those invariants holds trivially, so a
    topology that ships one cannot demonstrate the part of this system that
    took the most work. The concurrency tests prove it against a real
    database; this is the version somebody can watch.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    workers = {
        name: service
        for name, service in services.items()
        if name.startswith("task-worker")
    }
    assert len(workers) == 2, "the demo topology is a multi-worker one"


def test_neither_worker_pins_a_worker_id() -> None:
    """Each process mints its own at startup.

    An id set here would be an id two replicas could share, which is exactly
    the collision the lease is there to make impossible.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    for name, service in services.items():
        if not name.startswith("task-worker"):
            continue
        environment = service.get("environment") or {}
        assert "AW_WORKER_ID" not in environment
        assert not any(key.endswith("WORKER_ID") for key in environment)


def test_the_api_serves_the_console_from_the_image() -> None:
    """A stack somebody can open, rather than routes somebody has to know.

    The console is copied into the image and served same-origin; the API
    refuses to start when the directory is missing, so a broken image fails at
    startup rather than in a browser.
    """

    assert "COPY --chown=app:app web ./web" in (ROOT / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "--web-dir /app/web" in (ROOT / "docker/run-api-local.sh").read_text(
        encoding="utf-8"
    )
