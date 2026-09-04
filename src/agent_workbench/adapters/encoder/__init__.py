"""The encoder service, seen from a process that holds no weights (ADR-0106).

Three ports -- dense embedding, sparse encoding, reranking -- implemented as
HTTP clients of ``agent-encoder``, the one process in a deployment that loads
the models. Everything here is transport: the identity, the width and the
vocabulary a caller sees are the loaded model's own, fetched once at connect
time and refused if they disagree with the configuration, exactly as the
in-process adapters refuse a model that disagrees with it.
"""

from agent_workbench.adapters.encoder.client import (
    EncoderClient,
    EncoderServiceRefusedError,
    EncoderServiceUnavailableError,
    describe_service,
)
from agent_workbench.adapters.encoder.ports import (
    RemoteEmbedder,
    RemoteReranker,
    RemoteSparseEncoder,
    aclose_encoders,
)

__all__ = [
    "EncoderClient",
    "EncoderServiceRefusedError",
    "EncoderServiceUnavailableError",
    "RemoteEmbedder",
    "RemoteReranker",
    "RemoteSparseEncoder",
    "aclose_encoders",
    "describe_service",
]
