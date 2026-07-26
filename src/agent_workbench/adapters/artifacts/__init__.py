"""Artifact store implementations.

The local filesystem store serves development, the demo and the deterministic
tests. An S3-compatible store arrives with the upload data plane, where
presigned transfer is what makes the difference worth having.
"""

from agent_workbench.adapters.artifacts.local import LocalArtifactStore

__all__ = ["LocalArtifactStore"]
