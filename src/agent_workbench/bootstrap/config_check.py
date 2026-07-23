"""Offline configuration validation entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from agent_workbench.bootstrap.settings import load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-config-check",
        description=(
            "Validate Agent Workbench configuration without connecting to "
            "external services."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Optional TOML overlay; the committed default is always loaded first.",
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
    settings = load_settings(
        config_file=args.config,
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
