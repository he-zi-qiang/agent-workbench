"""Offline configuration validation entry point."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from agent_workbench.bootstrap.paths import (
    DEFAULT_CONFIG_FILE,
    PRODUCTION_CONFIG_FILE,
    TEST_CONFIG_FILE,
)
from agent_workbench.bootstrap.settings import load_settings

PROFILE_CONFIG_FILES: dict[str, Path] = {
    "development": DEFAULT_CONFIG_FILE,
    "test": TEST_CONFIG_FILE,
    "production": PRODUCTION_CONFIG_FILE,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-config-check",
        description=(
            "Validate Agent Workbench configuration without connecting to "
            "external services."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--config",
        type=Path,
        help="Optional TOML overlay; the committed default is always loaded first.",
    )
    source.add_argument(
        "--profile",
        choices=tuple(PROFILE_CONFIG_FILES),
        help=(
            "Validate a committed profile overlay. This only validates "
            "configuration; it never starts an Adapter or external service."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Development/test dotenv file. Production refuses this option.",
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        help="Optional directory containing flat AW_SECTION__FIELD secret files.",
    )
    parser.add_argument(
        "--show-public-config",
        action="store_true",
        help="Include the redacted public configuration in JSON output.",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> dict[str, object]:
    """Validate configuration and return a logging-safe diagnostic payload."""

    args = _parser().parse_args(argv)
    config_file = (
        PROFILE_CONFIG_FILES[args.profile] if args.profile is not None else args.config
    )
    settings = load_settings(
        config_file=config_file,
        env_file=args.env_file,
        secrets_dir=args.secrets_dir,
    )
    payload: dict[str, object] = {
        "status": "ok",
        "environment": settings.app.environment,
        "deployment_scope": settings.app.deployment_scope,
        "config_schema_version": settings.app.config_schema_version,
        "architecture_baseline": settings.app.architecture_baseline,
        "startup_config_revision": settings.revision(),
        "run_semantics_template_revision": settings.run_semantics_revision(),
        "policy_identity": settings.policy_identity(),
    }
    if args.show_public_config:
        payload["settings"] = settings.public_config()
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    payload = run(argv)
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
