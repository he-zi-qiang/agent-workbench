"""Stable filesystem locations used during bootstrap.

A source checkout reads the auditable top-level ``config/`` directory. A built
wheel contains the same default TOML under ``agent_workbench/_config`` so an
installed process never guesses its repository root.
"""

from __future__ import annotations

from pathlib import Path


def _find_checkout_root(module_file: Path) -> Path | None:
    for candidate in module_file.resolve().parents:
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "config" / "config.default.toml").is_file()
        ):
            return candidate
    return None


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = _find_checkout_root(Path(__file__))
PROJECT_ROOT = CHECKOUT_ROOT or PACKAGE_ROOT
CONFIG_DIR = (
    CHECKOUT_ROOT / "config"
    if CHECKOUT_ROOT is not None
    else PACKAGE_ROOT / "_config"
)
DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.default.toml"
TEST_CONFIG_FILE = CONFIG_DIR / "config.test.toml"

__all__ = [
    "CHECKOUT_ROOT",
    "CONFIG_DIR",
    "DEFAULT_CONFIG_FILE",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "TEST_CONFIG_FILE",
]
