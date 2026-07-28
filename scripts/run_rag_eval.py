"""Index the fixed corpus, ask the gold questions, write a report per retriever.

Runs dense and hybrid over the same corpus, the same gold set and the same
questions, so a difference between the two reports is a difference in
retrieval. Each arm gets its own collection because sparse changes the index
identity -- sharing one would mean the dense run reading points built for the
hybrid one.

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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qdrant_client import AsyncQdrantClient

from agent_workbench.adapters.embedding.bge import BgeM3Embedder
from agent_workbench.adapters.embedding.bge_sparse import BgeM3SparseEncoder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.reranking.bge_reranker import BgeReranker
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
# What each arm proposes before fusion, mirroring RetrievalService's
# candidate_multiplier rather than inventing a second policy here.
CANDIDATES = TOP_K * 4
VOCABULARY = 250002


async def _measure(
    embedder: BgeM3Embedder,
    sparse: BgeM3SparseEncoder | None,
    client: AsyncQdrantClient,
    reranker: BgeReranker | None = None,
) -> Any:
    """Index the corpus and score the gold set with one retriever.

    The reranked arm asks the index for CANDIDATES rather than TOP_K and cuts
    to TOP_K after scoring. That is not a second variable slipped into the
    comparison -- it is what reranking is. A reranker handed exactly TOP_K
    results can reorder them and never change which documents are returned,
    so recall would be identical by construction and the measurement would be
    of nothing. RetrievalService does the same thing for the same reason.
    """

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
            sparse_encoder=sparse,
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

        # A reranker needs something to choose from; the other arms are
        # measured at exactly the depth they answer at.
        _limit = CANDIDATES if reranker is not None else TOP_K

        async def retrieve(question: str) -> Sequence[str]:
            vector = await embedder.embed_query(question)
            if sparse is None:
                hits = await index.search(
                    vector=vector,
                    tenant_id=TENANT,
                    knowledge_base_id=KB,
                    authorized_principals=(OWNER,),
                    limit=_limit,
                )
            else:
                weights = await sparse.encode_query(question)
                hits = await index.search_hybrid(
                    vector=vector,
                    sparse_indices=weights.indices,
                    sparse_values=weights.values,
                    tenant_id=TENANT,
                    knowledge_base_id=KB,
                    authorized_principals=(OWNER,),
                    limit=_limit,
                    # Each arm proposes a full candidate set and RRF narrows
                    # them; truncating both to TOP_K first makes fusion choose
                    # between two already-shortened lists, which is a different
                    # retriever from the one being measured. RetrievalService
                    # says the same thing in a comment -- and this script did
                    # the opposite, which is what the first run of the expanded
                    # corpus actually measured.
                    dense_limit=CANDIDATES,
                    sparse_limit=CANDIDATES,
                )
            if reranker is not None:
                scores = await reranker.rerank(
                    question, tuple(hit.text for hit in hits)
                )
                # Descending, ties broken by the retriever's order, exactly as
                # RetrievalService does -- a report produced by a different
                # tie-break would not describe the code being shipped.
                order = sorted(range(len(hits)), key=lambda i: (-scores[i], i))
                hits = [hits[i] for i in order][:TOP_K]
            seen: list[str] = []
            for hit in hits:
                if hit.document_id not in seen:
                    seen.append(hit.document_id)
            return seen

        return await evaluate_retrieval(
            load_gold_set(GOLD),
            index_identity=service.index_identity,
            retrieve=retrieve,
        )
    finally:
        await client.delete_collection(collection)


async def main() -> int:
    url = os.environ.get("AGENT_WORKBENCH_TEST_QDRANT_URL")
    if not url:
        print("AGENT_WORKBENCH_TEST_QDRANT_URL is not set")
        return 2

    embedder = BgeM3Embedder.load(
        model_id="BAAI/bge-m3", revision="main", expected_dimension=1024
    )
    sparse = BgeM3SparseEncoder.load(
        model_id="BAAI/bge-m3",
        revision="main",
        expected_vocabulary_size=VOCABULARY,
    )

    # Skipped rather than fatal. The reranked arm needs a second set of
    # weights, and a machine that has the embedder but not the reranker should
    # still be able to produce the dense/hybrid comparison -- what it must not
    # do is emit a three-arm report with one arm quietly missing, so the skip
    # is printed and the report file for that arm is not written.
    reranker: BgeReranker | None
    try:
        reranker = BgeReranker.load(
            model_id="BAAI/bge-reranker-v2-m3", revision="main", batch_size=8
        )
    except Exception as unavailable:
        reranker = None
        print(
            f"SKIPPING hybrid-rerank: {type(unavailable).__name__}: {unavailable}",
            file=sys.stderr,
        )

    client = AsyncQdrantClient(url=url)
    REPORTS.mkdir(parents=True, exist_ok=True)
    arms: tuple[tuple[str, BgeM3SparseEncoder | None, BgeReranker | None], ...] = (
        ("dense", None, None),
        ("hybrid", sparse, None),
        *((("hybrid-rerank", sparse, reranker),) if reranker is not None else ()),
    )
    try:
        for name, encoder, cross in arms:
            report = await _measure(embedder, encoder, client, cross)
            (REPORTS / f"{name}.json").write_text(
                report.to_json() + "\n", encoding="utf-8"
            )
            print(f"--- {name} ---")
            print(report.to_json())
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
