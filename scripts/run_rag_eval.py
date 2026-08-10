"""Index the fixed corpus, ask the gold questions, write a report per retriever.

Runs dense and hybrid over the same corpus, the same gold set and the same
questions, so a difference between the two reports is a difference in
retrieval. Each arm gets its own collection because sparse changes the index
identity -- sharing one would mean the dense run reading points built for the
hybrid one.

Since ADR-017 each arm is also measured **twice**: once through the reference
retriever and once through the LlamaIndex one. That is step 2 of the migration
rules -- the same gold set over both paths -- and the comparison is only worth
anything because of what is held fixed. Both retrievers read the *same*
collection, embedded by the same model, with the same candidate budget and the
same top_k, so a difference between their two reports cannot be a difference in
what was indexed. Two collections would have made an equivalence result
unfalsifiable: any disagreement could be blamed on the index.

An equivalence report is a weak claim by construction, and worth stating as
such. Both paths call the same ``VectorIndexPort``, so identical scores confirm
that the framework did not perturb ordering, budgets or the mapping back to
chunks -- not that LlamaIndex is "as good as" the old code at anything it is
not being asked to do here. What it would catch is real, though: a top_k
silently applied twice, a fusion performed a second time in process, a page or
revision lost in the node round trip. Each of those moves the numbers.

Run locally with the embedding extra installed and a Qdrant reachable:

    AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \
    uv run --extra embedding python scripts/run_rag_eval.py

``--paths`` narrows which retrievers are measured; the default is all of them,
so an unflagged run is byte-for-byte the run this script always performed.
Narrowing exists because the two paths cost twice the wall clock, and a
question like "does hybrid fail cross-document questions" is about retrieval
quality rather than about the adapter -- one production path answers it. What
narrowing *cannot* do is produce equivalence evidence: with fewer than two
paths the ADR-017 step-2 comparison has nothing to compare, and the run says
so out loud rather than exiting zero as though the paths had agreed.

CI does not run this. It has no embedding runtime, and a report produced with
the deterministic embedder would be a measurement of a hash function.
"""

from __future__ import annotations

import argparse
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
from agent_workbench.adapters.llama_index import LlamaIndexCandidateRetriever
from agent_workbench.adapters.reranking.bge_reranker import BgeReranker
from agent_workbench.adapters.retrieval import ReferenceVectorIndexRetriever
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

#: Both ``CandidateRetrieverPort`` implementations, measured over one index.
#: The key becomes part of the report filename, so the two are never one file
#: overwriting the other -- which is how an "equivalence" result gets produced
#: by a second run of the same path.
RETRIEVERS: dict[str, Any] = {
    "reference": ReferenceVectorIndexRetriever,
    "llama_index": LlamaIndexCandidateRetriever,
}

#: Scores that measure the clock rather than the ranking. Excluded from the
#: equivalence check below; reported, not compared.
TIMING_METRICS = frozenset({"retrieval_latency_ms"})


async def _measure(
    embedder: BgeM3Embedder,
    sparse: BgeM3SparseEncoder | None,
    client: AsyncQdrantClient,
    reranker: BgeReranker | None = None,
    paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Index the corpus once, then score the gold set with each retriever.

    One collection, every selected retriever. Re-indexing per retriever would
    give each its own points and make any disagreement attributable to the
    index instead of to the path being measured -- which is the one thing this
    comparison exists to rule out. That holds however many paths ``paths``
    names: an empty tuple means all of them.

    The reranked arm asks for CANDIDATES rather than TOP_K and cuts to TOP_K
    after scoring. That is not a second variable slipped into the comparison --
    it is what reranking is. A reranker handed exactly TOP_K results can
    reorder them and never change which documents are returned, so recall would
    be identical by construction and the measurement would be of nothing.
    RetrievalService does the same thing for the same reason.
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

        # Every arm asks for CANDIDATES and cuts to TOP_K afterwards, which is
        # what RetrievalService does: ask wide, authorize, rerank, then narrow.
        #
        # It did not always. Before the port existed this script asked Qdrant
        # for TOP_K while telling it to prefetch CANDIDATES from each arm, so
        # fusion ran over two full lists and returned the best three.
        # `CandidateRetrieverPort` has one `limit`, so routing through it made
        # the prefetch equal to the answer depth -- three per arm -- and RRF
        # started choosing between two already-shortened lists. That is a
        # different retriever, and it measured as one: hybrid fell from 1.000
        # to 0.969 MRR on both paths at once, which is what made it legible as
        # a harness defect rather than an adapter regression.
        limit = CANDIDATES

        reports: dict[str, Any] = {}
        selected = paths or tuple(RETRIEVERS)
        for path_name in selected:
            build = RETRIEVERS[path_name]
            retriever = build(embedder=embedder, index=index, sparse_encoder=sparse)

            async def retrieve(
                question: str, retriever: Any = retriever
            ) -> Sequence[str]:
                hits = list(
                    await retriever.candidates(
                        query=question,
                        tenant_id=TENANT,
                        principal_id=OWNER,
                        knowledge_base_id=KB,
                        limit=limit,
                    )
                )
                if reranker is not None:
                    scores = await reranker.rerank(
                        question, tuple(hit.text for hit in hits)
                    )
                    # Descending, ties broken by the retriever's order, exactly
                    # as RetrievalService does -- a report produced by a
                    # different tie-break would not describe the code being
                    # shipped.
                    order = sorted(range(len(hits)), key=lambda i: (-scores[i], i))
                    hits = [hits[i] for i in order]
                # After reranking, never before: a reranker handed exactly
                # TOP_K results can reorder them but never change which
                # documents come back, so recall would be identical by
                # construction and the arm would measure nothing.
                hits = hits[:TOP_K]
                seen: list[str] = []
                for hit in hits:
                    if hit.document_id not in seen:
                        seen.append(hit.document_id)
                return seen

            reports[path_name] = await evaluate_retrieval(
                load_gold_set(GOLD),
                # The retriever's own name joins the index identity, so two
                # reports cannot be mistaken for two runs of one path.
                index_identity=f"{service.index_identity} via {retriever.mode}",
                retrieve=retrieve,
            )
        return reports
    finally:
        await client.delete_collection(collection)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        default=",".join(RETRIEVERS),
        help=(
            "Comma-separated retriever paths to measure "
            f"({', '.join(RETRIEVERS)}). Defaults to all of them. Fewer than "
            "two disables the ADR-017 equivalence comparison, which the run "
            "reports rather than passing silently."
        ),
    )
    arguments = parser.parse_args()
    paths = tuple(name.strip() for name in arguments.paths.split(",") if name.strip())
    unknown = sorted(set(paths) - set(RETRIEVERS))
    if unknown:
        print(
            f"unknown path(s): {', '.join(unknown)}; "
            f"choose from {', '.join(RETRIEVERS)}",
            file=sys.stderr,
        )
        return 2
    if not paths:
        print("--paths selected nothing to measure", file=sys.stderr)
        return 2

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

    # Loaded only when the arm that uses it is asked for, and that arm is now
    # opt-in. Both halves of this are the result of a measurement rather than a
    # preference.
    #
    # The reranker is a third set of BGE-M3-sized weights on top of the
    # embedder and the lexical encoder -- around 6.6 GB of models for a run in
    # which two arms out of three never touch the third. On a machine already
    # in swap that is not a small waste: a 2026-08-03 run held all three
    # resident and Qdrant's own counters put the hybrid arm at roughly *one
    # query per 50 seconds*, against the 7.2 s/question median this same
    # script recorded on 2026-07-28. Nothing about retrieval had changed; the
    # weights were being paged back in for every forward pass. Eagerly loading
    # a model an arm does not use makes that arm's numbers a measurement of the
    # page cache.
    #
    # It is opt-in rather than merely lazy because the arm is already known to
    # be uninformative here, and the README says so: hybrid scores 1.000 across
    # the 38-question gold set, so a reranker reordering a perfect ranking has
    # a delta of zero by construction. Running it costs an hour to confirm
    # arithmetic. Set AGENT_WORKBENCH_EVAL_RERANK=1 when there is a harder gold
    # set for it to say something about.
    reranker: BgeReranker | None = None
    if os.environ.get("AGENT_WORKBENCH_EVAL_RERANK") == "1":
        # Skipped rather than fatal. A machine that has the embedder but not
        # the reranker should still produce the dense/hybrid comparison -- what
        # it must not do is emit a three-arm report with one arm quietly
        # missing, so the skip is printed and that arm's report is not written.
        try:
            reranker = BgeReranker.load(
                model_id="BAAI/bge-reranker-v2-m3", revision="main", batch_size=8
            )
        except Exception as unavailable:
            print(
                f"SKIPPING hybrid-rerank: {type(unavailable).__name__}: {unavailable}",
                file=sys.stderr,
            )
    else:
        print(
            "SKIPPING hybrid-rerank: set AGENT_WORKBENCH_EVAL_RERANK=1 to run it",
            file=sys.stderr,
        )

    client = AsyncQdrantClient(url=url)
    REPORTS.mkdir(parents=True, exist_ok=True)
    arms: tuple[tuple[str, BgeM3SparseEncoder | None, BgeReranker | None], ...] = (
        ("dense", None, None),
        ("hybrid", sparse, None),
        *((("hybrid-rerank", sparse, reranker),) if reranker is not None else ()),
    )
    divergent = 0
    # Said once, before any numbers appear, because the thing a narrowed run
    # cannot produce is exactly the thing a reader skimming its output would
    # assume it did (ADR-017 step 2). A run that quietly exited zero on one
    # path would read as "the paths agreed".
    if len(paths) < 2:
        print(
            f"NOT COMPARING PATHS: measuring only {', '.join(paths)}; "
            "the ADR-017 equivalence check needs at least two and did not run",
            file=sys.stderr,
        )
    try:
        for name, encoder, cross in arms:
            reports = await _measure(embedder, encoder, client, cross, paths=paths)
            for path_name, report in reports.items():
                (REPORTS / f"{name}-{path_name}.json").write_text(
                    report.to_json() + "\n", encoding="utf-8"
                )
                print(f"--- {name} / {path_name} ---")
                print(report.to_json())

            # Stated by the script rather than left to whoever reads two files.
            # "The numbers look the same" is a claim somebody makes by eye; a
            # comparison the run itself performed is one it can be held to.
            #
            # Guarded on the count rather than left to `quality[1:]` being
            # empty. Both spellings "pass" on one path, and only this one says
            # which question was never asked -- a check that cannot fail must
            # not look like a check that passed.
            #
            # Quality metrics only. `retrieval_latency_ms` is wall clock and
            # never repeats to the last float, so comparing the whole score
            # dict would report every run as divergent -- a check that always
            # fires is a check nobody reads. Latency between the paths is still
            # worth knowing and is printed above; it is not evidence about
            # which documents came back.
            if len(reports) < 2:
                continue
            quality = [
                {
                    metric: value
                    for metric, value in report.scores.items()
                    if metric not in TIMING_METRICS
                }
                for report in reports.values()
            ]
            if any(other != quality[0] for other in quality[1:]):
                divergent += 1
                print(f"!!! {name}: the retrieval paths did not agree")
    finally:
        await client.close()

    if divergent:
        print(
            f"{divergent} arm(s) diverged across the measured paths; "
            "ADR-017 step 3 must not proceed on this evidence",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
