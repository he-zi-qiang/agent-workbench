"""hybrid versus hybrid+graph, over one index and one gold set.

Separate from ``run_rag_eval.py`` on purpose. That script has no database
dependency at all, and adding one so a third arm could run would make every
existing ablation need PostgreSQL to measure dense against sparse. This one
needs a database, a model and an API key, and says so by being its own file.

The order matters and is the whole method:

1. index the corpus once -- both arms read the same points, so a difference
   between them cannot be a difference in what was indexed;
2. extract the graph over that same corpus with a real model;
3. **report the extraction yield before measuring anything.** A graph with no
   rows makes "the graph did not help" and "the graph was not there" produce
   identical numbers, which is the one failure this comparison cannot detect
   from its own output;
4. measure both arms.

Run locally:

    AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \\
    AGENT_WORKBENCH_TEST_DSN=postgresql+asyncpg://... \\
    AW_SECRETS__DEEPSEEK_API_KEY=sk-… \\
    .venv/bin/python scripts/run_graph_ablation.py

CI does not run this: no embedding runtime, no model, and a report produced
with the deterministic embedder would measure a hash function.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_workbench.adapters.embedding.bge import BgeM3Embedder  # noqa: E402
from agent_workbench.adapters.embedding.bge_sparse import (  # noqa: E402
    BgeM3SparseEncoder,
)
from agent_workbench.adapters.events import ScopedEventSink  # noqa: E402
from agent_workbench.adapters.ingestion.approximate_counter import (  # noqa: E402
    ApproximateTokenCounter,
)
from agent_workbench.adapters.ingestion.parser import TextDocumentParser  # noqa: E402
from agent_workbench.adapters.memory.event_log import InMemoryEventLog  # noqa: E402
from agent_workbench.adapters.models.deepseek import (  # noqa: E402
    DeepSeekModel,
    DeepSeekProfile,
)
from agent_workbench.adapters.persistence import create_query_engine  # noqa: E402
from agent_workbench.adapters.persistence.knowledge_graph import (  # noqa: E402
    PostgresKnowledgeGraphStore,
)
from agent_workbench.adapters.policy.envelope import (  # noqa: E402
    EnvelopePolicyEngine,
)
from agent_workbench.adapters.retrieval.reference import (  # noqa: E402
    ReferenceVectorIndexRetriever,
)
from agent_workbench.adapters.retrieval.seed_expansion import (  # noqa: E402
    SeedExpansionRetriever,
)
from agent_workbench.adapters.tools import StaticToolRegistry  # noqa: E402
from agent_workbench.adapters.vector.qdrant import QdrantVectorIndex  # noqa: E402
from agent_workbench.application.chunking import Chunker  # noqa: E402
from agent_workbench.application.graph_enrichment import (  # noqa: E402
    GraphEnrichmentService,
)
from agent_workbench.application.graph_extraction import (  # noqa: E402
    GraphExtractionService,
    graph_identity,
)
from agent_workbench.application.ingestion import (  # noqa: E402
    IngestionRequest,
    IngestionService,
)
from agent_workbench.apps.ingestion_worker.identity import (  # noqa: E402
    restore_document_owner,
)
from agent_workbench.evaluation.runner import (  # noqa: E402
    evaluate_retrieval,
    load_gold_set,
)
from agent_workbench.runtime.agent_runtime import ClaudeLikeAgentRuntime  # noqa: E402
from agent_workbench.runtime.tool_gateway import ToolGateway  # noqa: E402

CORPUS = PROJECT_ROOT / "evals" / "rag" / "corpus"
GOLD = PROJECT_ROOT / "evals" / "rag" / "gold.jsonl"
REPORTS = PROJECT_ROOT / "evals" / "rag" / "reports"

TENANT = "tenant_eval"
KB = "kb_eval"
OWNER = "user_eval"

TOP_K = 3
CANDIDATES = TOP_K * 4
VOCABULARY = 250002

EXTRACTION_MODEL = "deepseek-chat"
PROMPT_VERSION = "v1"


async def _documents_for(retriever: Any, question: str) -> tuple[str, ...]:
    hits = await retriever.candidates(
        query=question,
        tenant_id=TENANT,
        principal_id=OWNER,
        knowledge_base_id=KB,
        limit=CANDIDATES,
    )
    seen: list[str] = []
    for hit in list(hits)[:TOP_K]:
        if hit.document_id not in seen:
            seen.append(hit.document_id)
    return tuple(seen)


async def main() -> int:
    url = os.environ.get("AGENT_WORKBENCH_TEST_QDRANT_URL")
    dsn = os.environ.get("AGENT_WORKBENCH_TEST_DSN")
    api_key = os.environ.get("AW_SECRETS__DEEPSEEK_API_KEY")
    if not url or not dsn or not api_key:
        print(
            "needs AGENT_WORKBENCH_TEST_QDRANT_URL, AGENT_WORKBENCH_TEST_DSN "
            "and AW_SECRETS__DEEPSEEK_API_KEY",
            file=sys.stderr,
        )
        return 2

    embedder = BgeM3Embedder.load(
        model_id="BAAI/bge-m3", revision="main", expected_dimension=1024
    )
    sparse = BgeM3SparseEncoder.load(
        model_id="BAAI/bge-m3",
        revision="main",
        expected_vocabulary_size=VOCABULARY,
    )
    identity = graph_identity(
        extraction_model=EXTRACTION_MODEL,
        prompt_version=PROMPT_VERSION,
        embedder_identity=embedder.identity,
    )

    collection = f"ablation_{uuid.uuid4().hex}"
    client = AsyncQdrantClient(url=url)
    engine = create_query_engine(dsn, application_name="graph-ablation")
    http = httpx.AsyncClient(timeout=60.0)
    try:
        index = QdrantVectorIndex(client, collection=collection)
        await index.ensure_collection(vector_size=embedder.dimension)
        ingestion = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=512, overlap_tokens=64, counter=ApproximateTokenCounter()
            ),
            embedder=embedder,
            index=index,
            sparse_encoder=sparse,
        )

        print("indexing the corpus ...", flush=True)
        versions: list[tuple[str, str, bytes]] = []
        for path in sorted(CORPUS.glob("*.md")):
            document_id = f"doc_{path.stem}"
            version_id = f"ver_{path.stem}"
            content = path.read_bytes()
            await ingestion.ingest(
                IngestionRequest(
                    tenant_id=TENANT,
                    knowledge_base_id=KB,
                    document_id=document_id,
                    document_version=version_id,
                    owner_id=OWNER,
                    authorized_principals=(OWNER,),
                    source_revision=1,
                    media_type="text/markdown",
                    content=content,
                )
            )
            versions.append((document_id, version_id, content))
        print(f"  {len(versions)} documents indexed", flush=True)

        # --- the graph, over the same corpus -----------------------------
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM kg_relations WHERE tenant_id = :t"),
                {"t": TENANT},
            )
            await connection.execute(
                text("DELETE FROM kg_mentions WHERE tenant_id = :t"), {"t": TENANT}
            )
            await connection.execute(
                text("DELETE FROM kg_entities WHERE tenant_id = :t"), {"t": TENANT}
            )

        store = PostgresKnowledgeGraphStore(engine)
        # Temperature nailed to zero, as run_chat_eval does: a comparison
        # whose extraction sampled differently would report the sampler.
        profile = DeepSeekProfile(
            model_id=EXTRACTION_MODEL, temperature=0.0, timeout_seconds=120.0
        )
        model = DeepSeekModel(
            client=http,
            api_key=api_key,
            base_url=os.environ.get("AW_MODEL__BASE_URL", "https://api.deepseek.com"),
            profiles={"main": profile, "compact": profile},
        )
        empty = StaticToolRegistry([])
        runtime = ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=empty, policy=EnvelopePolicyEngine(registry=empty)
            ),
            policy_identity="graph-ablation",
            model_label=EXTRACTION_MODEL,
        )
        enrichment = GraphEnrichmentService(
            ingestion=ingestion,
            extraction=GraphExtractionService(
                executor=runtime,
                timeout_seconds=90.0,
                sink_for=lambda stream_id: ScopedEventSink(
                    log=InMemoryEventLog(),
                    scope=_scope(stream_id),
                ),
            ),
            store=store,
            graph_identity=identity,
        )

        print("extracting the graph ...", flush=True)
        chunks = entities = relations = unreadable = 0
        for document_id, version_id, content in versions:
            report = await enrichment.enrich(
                restore_document_owner(tenant_id=TENANT, owner_id=OWNER),
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id=document_id,
                document_version=version_id,
                media_type="text/markdown",
                content=content,
            )
            chunks += report.chunks
            entities += report.entities
            relations += report.relations
            unreadable += report.unreadable_chunks

        async with engine.connect() as connection:
            mentions = int(
                (
                    await connection.execute(
                        text("SELECT count(*) FROM kg_mentions WHERE tenant_id = :t"),
                        {"t": TENANT},
                    )
                ).scalar_one()
            )
        yield_report = {
            "chunks": chunks,
            "entity_writes": entities,
            "relations": relations,
            "unreadable_chunks": unreadable,
            "mentions_stored": mentions,
        }
        print(f"  yield: {json.dumps(yield_report)}", flush=True)

        # The check this comparison cannot make from its own numbers.
        if mentions == 0:
            print(
                "REFUSING TO MEASURE: the graph has no mentions, so the two arms "
                "would differ only by an arm that cannot contribute -- 'the graph "
                "did not help' and 'the graph was not there' are the same result",
                file=sys.stderr,
            )
            return 1
        if unreadable:
            print(
                f"NOTE: {unreadable} chunk(s) could not be read by the extractor; "
                "the graph is thinner than the corpus",
                file=sys.stderr,
            )

        # --- the two arms, over one index --------------------------------
        control = ReferenceVectorIndexRetriever(
            embedder=embedder, index=index, sparse_encoder=sparse
        )
        treatment = SeedExpansionRetriever(
            embedder=embedder,
            index=index,
            graph=store,
            graph_identity=identity,
            sparse_encoder=sparse,
        )

        gold = load_gold_set(GOLD)
        results: dict[str, Any] = {"yield": yield_report}
        for name, retriever in (("hybrid", control), ("hybrid+graph", treatment)):
            print(f"measuring {name} ...", flush=True)

            async def retrieve(question: str, r: Any = retriever) -> tuple[str, ...]:
                return await _documents_for(r, question)

            report = await evaluate_retrieval(
                gold,
                index_identity=f"{ingestion.index_identity} via {retriever.mode}",
                retrieve=retrieve,
            )
            (REPORTS / f"graph-ablation-{name}.json").write_text(
                report.to_json() + "\n", encoding="utf-8"
            )
            (REPORTS / f"graph-ablation-{name}-outcomes.json").write_text(
                report.outcomes_to_json() + "\n", encoding="utf-8"
            )
            results[name] = report.scores
            print(f"--- {name} ---")
            print(report.to_json())

        (REPORTS / "graph-ablation.json").write_text(
            json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    finally:
        await client.delete_collection(collection)
        await client.close()
        await engine.dispose()
        await http.aclose()


def _scope(stream_id: str) -> Any:
    from agent_workbench.ports.event_log import EventScope

    return EventScope(stream_id=stream_id, run_id=stream_id)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
