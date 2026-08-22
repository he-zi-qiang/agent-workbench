"""Filesystem-backed adapters.

Separate from ``adapters/artifacts``: that package stores content-addressed
blobs whose names this project chooses, and this one operates on a directory
tree whose names the *user* chose (ADR-072). The two have opposite threat
models, and keeping them in one package is how the artifact store's "there is
no way to offer a caller-chosen path" would eventually acquire one.
"""

from agent_workbench.adapters.filesystem.sandbox import (
    ProjectSandbox,
    ProjectSandboxError,
)

__all__ = ["ProjectSandbox", "ProjectSandboxError"]
