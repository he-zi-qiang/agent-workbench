# Hybrid fusion

Dense retrieval finds passages by meaning; sparse retrieval finds them by term
overlap. Agent Workbench fuses the two exactly once, and that fusion happens
**in the application process**, not in the vector store: the adapter issues the
two single-arm queries concurrently, each arm is ordered deterministically by
`(-score, chunk_id)`, and reciprocal rank fusion is computed over those two
rank lists. The retrieval adapter never re-ranks by relative score afterwards,
because fusing twice invents an ordering that neither retriever produced.

Qdrant serves each arm. It does not combine them. An earlier design did ask
Qdrant's Query API to fuse, and it was replaced because ties inside an arm had
no defined order there, so the same query could return two different rankings.
