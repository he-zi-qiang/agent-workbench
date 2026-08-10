# Configuration: rag

`rag.ingestion.chunk_size_tokens` and `rag.ingestion.chunk_overlap_tokens`
decide window geometry. `rag.embedding.model_id` and `rag.embedding.revision`
pin the model. `rag.reranker.enabled` turns the cross-encoder on.

Every key under `rag` enters the Task semantics snapshot, so changing one
changes what newly submitted Tasks mean.
