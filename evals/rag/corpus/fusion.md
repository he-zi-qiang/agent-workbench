# Hybrid fusion

Dense retrieval finds passages by meaning; sparse retrieval finds them by term
overlap. Agent Workbench fuses the two exactly once, inside Qdrant's Query API,
using reciprocal rank fusion. The retrieval adapter maps the fused result and
never re-ranks by relative score, because fusing twice invents an ordering that
neither retriever produced.
