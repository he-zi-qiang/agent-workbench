"""Ask the same questions down both Chat paths, and report what the second buys.

Two shapes answer a question in this system. ``FixedTwoStepExecution`` retrieves
exactly once, using the user's question verbatim as the query, then answers from
what came back. ``AgenticExecution`` gives the model ``knowledge_search`` and a
budget, and lets it decide whether to search, what to search for, and how many
times. Both shipped; nothing had ever measured the difference.

Everything except the shape is held fixed: one corpus, one collection, one
embedder, one sparse encoder, one reranker, one model, one ``top_k``, one gold
set. A difference between the two reports is a difference in shape.

Scoring is deterministic and the gold set was written before either arm ran. No
LLM judge: ADR-006 puts deterministic tests ahead of online model ones, and
§12.4 is explicit that a judge does not replace a gold set. What that buys is a
number nobody can argue with; what it costs is that "did the answer say the
right thing" is approximated by "does the answer contain these terms". The terms
are taken from the corpus wording, not from any run's output.

Run locally with the embedding extra, a Qdrant, and a provider key:

    AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \\
    AW_SECRETS__DEEPSEEK_API_KEY=sk-… \\
    uv run --extra embedding python scripts/run_chat_eval.py

CI does not run this. It has no embedding runtime and calls no provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from qdrant_client import AsyncQdrantClient

from agent_workbench.adapters.embedding.bge import BgeM3Embedder
from agent_workbench.adapters.embedding.bge_sparse import BgeM3SparseEncoder
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.models.deepseek import DeepSeekModel, DeepSeekProfile
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.reranking.bge_reranker import BgeReranker
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.tools.knowledge_search import (
    SPEC as SEARCH_SPEC,
)
from agent_workbench.adapters.tools.knowledge_search import (
    TOOL_NAME as SEARCH_TOOL,
)
from agent_workbench.adapters.tools.knowledge_search import KnowledgeSearchTool
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat_execution import (
    AgenticExecution,
    ChatRequest,
    FixedTwoStepExecution,
    RetrievalJournal,
    TurnExecution,
)
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.documents import ReadableDocument
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.tools import ToolBinding
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals/rag/corpus"
GOLD = ROOT / "evals/chat/gold.jsonl"
REPORTS = ROOT / "evals/chat/reports"

TENANT = "tenant_eval"
KB = "kb_eval"
OWNER = "user_eval"

#: Same reasoning as the retrieval eval: with ten documents, a top_k large
#: enough to return most of them makes every question retrieve everything and
#: measures "the corpus is small" rather than the shape under test.
TOP_K = 3

#: What the fixed shape gets: one model call, no tools.
FIXED_BUDGET = RunBudget(max_steps=1, max_tool_calls=1)
#: What the agentic shape gets, mirroring the shipped defaults in
#: ``chat.max_agentic_steps`` / ``max_agentic_searches``.
AGENTIC_BUDGET = RunBudget(max_steps=4, max_tool_calls=6)

#: Every search the agentic arm made, in order, with the arguments the model
#: chose. Appended by the recording handler below.
SEARCH_CALLS: list[dict[str, Any]] = []


class _EvalDocuments:
    """Everything this eval indexed is readable by the eval principal.

    The real store re-checks every candidate against PostgreSQL, and that
    boundary has its own tests against a real database -- including the case
    this class cannot express, where a grant is withdrawn mid-answer. Standing
    a PostgreSQL up here would add a variable to a comparison whose whole point
    is that only the shape differs.
    """

    def __init__(self, document_ids: Sequence[str]) -> None:
        self._ids = frozenset(document_ids)

    async def readable_versions(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        document_ids: tuple[str, ...],
    ) -> tuple[ReadableDocument, ...]:
        del tenant_id, principal_id
        return tuple(
            ReadableDocument(
                document_id=document_id,
                knowledge_base_id=KB,
                source_revision=1,
            )
            for document_id in document_ids
            if document_id in self._ids
        )


@dataclass
class Answer:
    """One question, down one arm."""

    question_id: str
    family: str
    answer: str
    citations: tuple[str, ...] = ()  # chunk ids
    cited_documents: tuple[str, ...] = ()
    fabricated: int = 0
    searches: int = 0
    model_calls: int = 0
    tool_failures: int = 0
    tokens: int = 0
    seconds: float = 0.0
    status: str = "completed"
    stop_reason: str = ""


@dataclass
class Score:
    """What the gold set says about one answer."""

    question_id: str
    family: str
    facts_found: int
    facts_expected: int
    complete: bool
    clean_abstention: bool | None
    citation_precision: float | None
    citation_recall: float | None


@dataclass
class ArmReport:
    name: str
    answers: list[Answer] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------


async def _index(
    embedder: BgeM3Embedder,
    sparse: BgeM3SparseEncoder,
    client: AsyncQdrantClient,
    collection: str,
) -> tuple[str, ...]:
    index = QdrantVectorIndex(client, collection=collection)
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
    document_ids: list[str] = []
    for path in sorted(CORPUS.glob("*.md")):
        document_id = f"doc_{path.stem}"
        await service.ingest(
            IngestionRequest(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id=document_id,
                document_version=f"ver_{path.stem}",
                owner_id=OWNER,
                authorized_principals=(OWNER,),
                source_revision=1,
                media_type="text/markdown",
                content=path.read_bytes(),
            )
        )
        document_ids.append(document_id)
    return tuple(document_ids)


# --------------------------------------------------------------------------
# The two arms
# --------------------------------------------------------------------------


def _runtime(model: DeepSeekModel, gateway: ToolGateway, label: str) -> Any:
    return ClaudeLikeAgentRuntime(
        model=model,
        gateway=gateway,
        policy_identity="eval",
        model_label=label,
    )


def _fixed(retrieval: RetrievalService, model: DeepSeekModel, label: str) -> Any:
    empty = StaticToolRegistry([])
    return FixedTwoStepExecution(
        retrieval=retrieval,
        executor=_runtime(
            model,
            ToolGateway(registry=empty, policy=EnvelopePolicyEngine(registry=empty)),
            label,
        ),
        budget=FIXED_BUDGET,
    )


def _agentic(
    retrieval: RetrievalService,
    model: DeepSeekModel,
    label: str,
    *,
    tool_timeout_seconds: int,
) -> tuple[Any, RetrievalJournal]:
    journal = RetrievalJournal()
    tool = KnowledgeSearchTool(retrieval=retrieval, journal=journal)

    async def recording_handler(invocation: Any) -> Any:
        """What the model actually asked for.

        Tool arguments never travel in events -- only a size and a digest -- so
        an aggregate cannot say whether a search that returned nothing was
        looking in the right place. This is the only way to see the query and
        the knowledge base the model chose.
        """

        SEARCH_CALLS.append(dict(invocation.call.arguments))
        return await tool.handle(invocation)

    # The only knob this eval turns, and it is turned for a stated reason: the
    # shipped spec allows a search 30 seconds, and on a machine where one
    # retrieval takes longer than that every search fails and the arm measures
    # the host rather than the shape. Both settings are reported.
    spec = SEARCH_SPEC.model_copy(update={"timeout_seconds": tool_timeout_seconds})
    binding = ToolBinding(spec=spec, handler=recording_handler)
    registry = StaticToolRegistry([binding])
    execution = AgenticExecution(
        executor=_runtime(
            model,
            ToolGateway(
                registry=registry, policy=EnvelopePolicyEngine(registry=registry)
            ),
            label,
        ),
        journal=journal,
        budget=AGENTIC_BUDGET,
        tool_names=(SEARCH_TOOL,),
    )
    return execution, journal


async def _ask(
    execution: TurnExecution, question: str, entry: dict[str, Any]
) -> Answer:
    log = InMemoryEventLog()
    run_id = f"run_{uuid.uuid4().hex}"
    sink = ScopedEventSink(log, EventScope(stream_id=f"eval_{run_id}", run_id=run_id))
    request = ChatRequest(
        session_id=f"ses_{uuid.uuid4().hex}",
        question=question,
        principal=PrincipalContext(
            tenant_id=TENANT,
            principal_id=OWNER,
            scopes=("knowledge:read",),
        ),
        knowledge_base_id=KB,
        idempotency_key=f"eval_{uuid.uuid4().hex}",
        top_k=TOP_K,
        run_id=run_id,
    )

    started = time.monotonic()
    produced = await execution.produce(
        request,
        history=(),
        sink=sink,
        cancellation=NullCancellationToken(),
    )
    elapsed = time.monotonic() - started

    recorded = await log.read(f"eval_{run_id}", limit=500)
    kinds = [envelope.event_type for envelope in recorded]
    usage = produced.outcome.usage.tokens
    return Answer(
        question_id=entry["id"],
        family=entry["family"],
        answer=produced.outcome.output_text or "",
        citations=tuple(citation.chunk_id for citation in produced.citations),
        cited_documents=tuple(
            sorted({citation.document_id for citation in produced.citations})
        ),
        fabricated=len(produced.fabricated_citations),
        # The fixed shape does not emit ToolCompleted -- it retrieves outside
        # the tool path -- so its search count is one by construction.
        searches=kinds.count("ToolCompleted")
        or (1 if kinds.count("ContextBuilt") else 0),
        model_calls=kinds.count("ModelCompleted"),
        tool_failures=kinds.count("ToolFailed"),
        tokens=usage.input_tokens + usage.output_tokens,
        seconds=round(elapsed, 1),
        status=produced.outcome.status,
        stop_reason=produced.outcome.stop_reason or "",
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _score(answer: Answer, entry: dict[str, Any]) -> Score:
    haystack = " ".join(answer.answer.lower().split())
    expected = [term.lower() for term in entry.get("must_contain", [])]
    found = sum(1 for term in expected if term in haystack)

    clean: bool | None = None
    if entry["family"] == "absent":
        forbidden = [term.lower() for term in entry.get("must_not_contain", [])]
        # Two ways to fail an absent question: assert a specific invented value,
        # or attach a citation to an answer the corpus does not support. The
        # second is the one an arm that searches more often has more chances at.
        clean = not any(term in haystack for term in forbidden) and not answer.citations

    relevant = set(entry.get("relevant", []))
    cited = set(answer.cited_documents)
    precision = len(cited & relevant) / len(cited) if cited else None
    recall = len(cited & relevant) / len(relevant) if relevant else None

    return Score(
        question_id=answer.question_id,
        family=answer.family,
        facts_found=found,
        facts_expected=len(expected),
        complete=bool(expected) and found == len(expected),
        clean_abstention=clean,
        citation_precision=precision,
        citation_recall=recall,
    )


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _summary(report: ArmReport) -> dict[str, Any]:
    graded = [s for s in report.scores if s.family != "absent"]
    absent = [s for s in report.scores if s.family == "absent"]
    answers = {a.question_id: a for a in report.answers}

    by_family: dict[str, Any] = {}
    for family in sorted({s.family for s in graded}):
        rows = [s for s in graded if s.family == family]
        by_family[family] = {
            "questions": len(rows),
            "complete": sum(1 for s in rows if s.complete),
            "fact_recall": _mean(
                [s.facts_found / s.facts_expected for s in rows if s.facts_expected]
            ),
            "citation_recall": _mean(
                [s.citation_recall for s in rows if s.citation_recall is not None]
            ),
        }

    graded_answers = [answers[s.question_id] for s in graded]
    return {
        "arm": report.name,
        "questions": len(report.scores),
        "complete": sum(1 for s in graded if s.complete),
        "fact_recall": _mean(
            [s.facts_found / s.facts_expected for s in graded if s.facts_expected]
        ),
        "citation_precision": _mean(
            [s.citation_precision for s in graded if s.citation_precision is not None]
        ),
        "citation_recall": _mean(
            [s.citation_recall for s in graded if s.citation_recall is not None]
        ),
        "clean_abstentions": (
            f"{sum(1 for s in absent if s.clean_abstention)}/{len(absent)}"
        ),
        "fabricated_citations": sum(a.fabricated for a in report.answers),
        "by_family": by_family,
        # Per question, because an aggregate cannot tell "the shape answered
        # badly" from "the run never reached an answer". The first calibration
        # reported 0.0 fact recall for the agentic arm and the number alone did
        # not say whether the model was wrong or had run out of steps.
        "searches_made": list(SEARCH_CALLS),
        "questions_detail": [
            {
                "id": s.question_id,
                "family": s.family,
                "complete": s.complete,
                "facts": f"{s.facts_found}/{s.facts_expected}",
                "status": answers[s.question_id].status,
                "stop_reason": answers[s.question_id].stop_reason,
                "searches": answers[s.question_id].searches,
                "model_calls": answers[s.question_id].model_calls,
                "cited": list(answers[s.question_id].cited_documents),
                "seconds": answers[s.question_id].seconds,
                "answer": answers[s.question_id].answer[:600],
            }
            for s in report.scores
        ],
        "cost": {
            "mean_searches": _mean([a.searches for a in graded_answers]),
            "mean_model_calls": _mean([a.model_calls for a in graded_answers]),
            "mean_tokens": _mean([a.tokens for a in graded_answers]),
            "mean_seconds": _mean([a.seconds for a in report.answers]),
            "tool_failures": sum(a.tool_failures for a in report.answers),
            "total_tokens": sum(a.tokens for a in report.answers),
        },
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


async def _run_arm(
    name: str, execution: TurnExecution, entries: Sequence[dict[str, Any]]
) -> ArmReport:
    report = ArmReport(name=name)
    for entry in entries:
        print(f"  [{name}] {entry['id']} …", flush=True)
        try:
            answer = await _ask(execution, entry["question"], entry)
        except Exception as error:  # one arm failing is a result, not a crash
            print(f"      raised {type(error).__name__}: {error}", flush=True)
            answer = Answer(
                question_id=entry["id"],
                family=entry["family"],
                answer="",
                status="failed",
                stop_reason=type(error).__name__,
            )
        report.answers.append(answer)
        report.scores.append(_score(answer, entry))
        print(
            f"      {answer.seconds}s · {answer.searches} search · "
            f"{answer.model_calls} model · {len(answer.citations)} cited",
            flush=True,
        )
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool-timeout-seconds",
        type=int,
        default=SEARCH_SPEC.timeout_seconds,
        help=(
            "What one knowledge_search may take in the agentic arm. The shipped "
            "spec says 30; a host where retrieval is slower than that measures "
            "itself rather than the shape."
        ),
    )
    parser.add_argument(
        "--arms",
        default="fixed,agentic",
        help="Comma-separated subset to run.",
    )
    parser.add_argument("--limit", type=int, default=0, help="First N questions only.")
    parser.add_argument(
        "--no-reranker",
        action="store_true",
        help=(
            "Retrieve with hybrid fusion and no cross-encoder. Three model sets "
            "resident at once need about 12 GB; a smaller host swaps instead of "
            "computing, and a report produced while swapping measures the host. "
            "Both arms still share one retriever, so the comparison holds -- what "
            "changes is that it describes the shapes over hybrid rather than over "
            "hybrid+rerank, and the report says which."
        ),
    )
    arguments = parser.parse_args()

    qdrant_url = os.environ.get("AGENT_WORKBENCH_TEST_QDRANT_URL")
    api_key = os.environ.get("AW_SECRETS__DEEPSEEK_API_KEY")
    if not qdrant_url or not api_key:
        print(
            "needs AGENT_WORKBENCH_TEST_QDRANT_URL and AW_SECRETS__DEEPSEEK_API_KEY",
            file=sys.stderr,
        )
        return 2

    entries = [
        json.loads(line)
        for line in GOLD.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if arguments.limit:
        entries = entries[: arguments.limit]

    model_id = os.environ.get("AW_MODEL__MAIN__MODEL_ID", "deepseek-chat")
    client = AsyncQdrantClient(url=qdrant_url, timeout=600)
    http = httpx.AsyncClient(timeout=180.0)
    collection = f"chateval_{uuid.uuid4().hex}"
    reports: list[dict[str, Any]] = []
    try:
        print("loading encoders …", flush=True)
        # Same weights and revisions the retrieval eval uses, so the two
        # reports describe the same retriever.
        embedder = BgeM3Embedder.load(
            model_id="BAAI/bge-m3", revision="main", expected_dimension=1024
        )
        sparse = BgeM3SparseEncoder.load(
            model_id="BAAI/bge-m3",
            revision="main",
            expected_vocabulary_size=250_002,
        )
        reranker = (
            None
            if arguments.no_reranker
            else BgeReranker.load(
                model_id="BAAI/bge-reranker-v2-m3", revision="main", batch_size=8
            )
        )
        print(f"indexing the corpus into {collection} …", flush=True)
        document_ids = await _index(embedder, sparse, client, collection)

        retrieval = RetrievalService(
            embedder=embedder,
            index=QdrantVectorIndex(client, collection=collection),
            documents=_EvalDocuments(document_ids),  # pyright: ignore[reportArgumentType]
            sparse_encoder=sparse,
            reranker=reranker,
        )
        # Temperature nailed to zero. A comparison whose two arms sampled
        # differently would report the sampler as often as the shape.
        profile = DeepSeekProfile(
            model_id=model_id, temperature=0.0, timeout_seconds=180.0
        )
        model = DeepSeekModel(
            client=http,
            api_key=api_key,
            base_url=os.environ.get("AW_MODEL__BASE_URL", "https://api.deepseek.com"),
            profiles={"main": profile, "compact": profile},
        )

        wanted = {name.strip() for name in arguments.arms.split(",") if name.strip()}
        if "fixed" in wanted:
            print("\narm: fixed (retrieve once, then answer)", flush=True)
            reports.append(
                _summary(
                    await _run_arm("fixed", _fixed(retrieval, model, model_id), entries)
                )
            )
        if "agentic" in wanted:
            print(
                f"\narm: agentic (model searches, {arguments.tool_timeout_seconds}s "
                "per search)",
                flush=True,
            )
            execution, _ = _agentic(
                retrieval,
                model,
                model_id,
                tool_timeout_seconds=arguments.tool_timeout_seconds,
            )
            reports.append(_summary(await _run_arm("agentic", execution, entries)))
    finally:
        # A leftover collection is untidy, not a result.
        with suppress(Exception):
            await client.delete_collection(collection)
        await client.close()
        await http.aclose()

    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "top_k": TOP_K,
        "model": model_id,
        "retriever": "hybrid" if arguments.no_reranker else "hybrid+rerank",
        "tool_timeout_seconds": arguments.tool_timeout_seconds,
        "questions": len(entries),
        "arms": reports,
    }
    suffix = "hybrid" if arguments.no_reranker else "hybrid-rerank"
    destination = REPORTS / f"chat-{suffix}-{arguments.tool_timeout_seconds}s.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("\n" + json.dumps(payload, indent=2))
    print(f"\nwritten to {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
