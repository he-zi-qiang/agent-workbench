"""Embedding adapters.

``BgeM3Embedder`` is re-exported by name only; importing this package must not
pull in the optional runtime, so the module is imported lazily by whoever
actually loads a model.
"""

from agent_workbench.adapters.embedding.fake import (
    DeterministicEmbedder,
    DeterministicSparseEncoder,
)

__all__ = ["DeterministicEmbedder", "DeterministicSparseEncoder"]
