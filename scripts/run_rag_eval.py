"""Index the fixed corpus, ask the gold questions, write a report.

Run locally with the embedding extra installed and a Qdrant reachable:

    AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \
    uv run --extra embedding python scripts/run_rag_eval.py

CI does not run this. It has no embedding runtime, and a report produced with
the deterministic embedder would be a measurement of a hash function.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qdrant_client import AsyncQdrantClient

from agent_workbench.adapters.embedding.bge import BgeM3Embedder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import (
    IngestionRequest,
    IngestionService,
)
from agent_workbench.evaluation import evaluate_retrieval, load_gold_set

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals/rag/corpus"
GOLD = ROOT / "evals/rag/gold.jsonl"
REPORTS = ROOT / "evals/rag/reports"

TENANT = "tenant_eval"
KB = "kb_eval"
OWNER = "user_eval"
# Smaller than the corpus on purpose. With six documents and a top_k of ten,
# every question retrieves every document, so recall@5 and recall@10 report
# "ten is more than six" rather than anything about ranking. Three forces the
# retriever to choose, which is the only condition under which recall means
# something.
TOP_K = 3


async def main() -> int:
    url = os.environ.get("AGENT_WORKBENCH_TEST_QDRANT_URL")
    if not url:
        print("AGENT_WORKBENCH_TEST_QDRANT_URL is not set")
        return 2

    embedder = BgeM3Embedder.load(
        model_id="BAAI/bge-m3", revision="main", expected_dimension=1024
    )
    client = AsyncQdrantClient(url=url)
    collection = f"eval_{uuid.uuid4().hex}"
    index = QdrantVectorIndex(client, collection=collection)

    try:
        await index.ensure_collection(vector_size=embedder.dimension)
        service = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=512, overlap_tokens=64, counter=ApproximateTokenCounter()
            ),
            embedder=embedder,
            index=index,
        )
        for path in sorted(CORPUS.glob("*.md")):
            await service.ingest(
                IngestionRequest(
                    tenant_id=TENANT,
                    knowledge_base_id=KB,
                    document_id=f"doc_{path.stem}",
                    document_version=f"ver_{path.stem}",
                    owner_id=OWNER,
                    authorized_principals=(OWNER,),
                    source_revision=1,
                    media_type="text/markdown",
                    content=path.read_bytes(),
                )
            )

        async def retrieve(question: str) -> Sequence[str]:
            hits = await index.search(
                vector=await embedder.embed_query(question),
                tenant_id=TENANT,
                knowledge_base_id=KB,
                authorized_principals=(OWNER,),
                limit=TOP_K,
            )
            # Ranked document ids, deduplicated: several chunks of one document
            # are one retrieval of that document, not several.
            seen: list[str] = []
            for hit in hits:
                if hit.document_id not in seen:
                    seen.append(hit.document_id)
            return seen

        report = await evaluate_retrieval(
            load_gold_set(GOLD),
            index_identity=f"{embedder.identity}+{service.chunker.identity}",
            retrieve=retrieve,
        )
    finally:
        await client.delete_collection(collection)
        await client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "dense.json").write_text(report.to_json() + "\n", encoding="utf-8")
    print(report.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
