"""Static validation of the checked-in local Compose topology."""

from __future__ import annotations

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
