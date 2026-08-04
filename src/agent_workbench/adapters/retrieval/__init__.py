"""Candidate retrievers: the implementations of ``CandidateRetrieverPort``.

Two of them, and they are not peers. ``ReferenceVectorIndexRetriever`` is the
path this project built before ADR-017 restored LlamaIndex as the primary RAG
framework; it stays because a migration needs something to be measured against,
and it is named so that nobody can mistake it for a second production path.
The LlamaIndex retriever lives one package over, behind that framework's own
boundary.
"""

from agent_workbench.adapters.retrieval.reference import ReferenceVectorIndexRetriever

__all__ = ["ReferenceVectorIndexRetriever"]
