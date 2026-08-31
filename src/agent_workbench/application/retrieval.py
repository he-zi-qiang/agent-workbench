"""Retrieval, with PostgreSQL deciding who may read what.

The vector index narrows; PostgreSQL authorizes. Those are different jobs and
this module is where the difference is enforced. A point's payload records the
ACL that was true when somebody last indexed the document, and "last indexed"
is not "now" -- a grant revoked a second ago has not reached the index yet, and
the index will happily return the chunk to the person it was taken from.

So every candidate is re-checked against PostgreSQL *before* it becomes context.
Not after ranking, not while assembling citations: before, because everything
downstream is a way for the text to escape. A chunk that reaches a reranker has
been read by a model; a chunk that reaches a citation has been shown to a user.

Authorization is checked twice, and the second time is not redundant. Between
building a context and committing an answer there is a model call, and a grant
can be withdrawn during it. The revision each document was authorized at is
carried through, and an answer whose sources have moved is refused rather than
delivered -- for the same reason the first check exists, one step later.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic

from agent_workbench.domain.context import (
    Citation,
    ContextChunk,
    ContextPacket,
    SourceLocator,
)
from agent_workbench.ports.candidates import CandidateRetrieverPort
from agent_workbench.ports.conversation_store import AuthorizedRevision
from agent_workbench.ports.documents import DocumentStore
from agent_workbench.ports.reranker import RerankerPort
from agent_workbench.ports.telemetry import (
    RETRIEVAL_AUTHORIZED,
    RETRIEVAL_CANDIDATES,
    RETRIEVAL_DURATION,
    NullTelemetry,
    Telemetry,
)
from agent_workbench.ports.vector_index import ScoredChunk

# Asked of the index, before authorization removes some of them. A candidate
# the caller may not read still costs a slot, so the funnel starts wider than
# it ends.
DEFAULT_CANDIDATE_MULTIPLIER = 4

# Matches the rag.reranker.timeout_seconds default. Stated here as well because
# this module must remain usable without the settings layer, and a service
# constructed without a timeout must still have one -- an unbounded wait on an
# optional quality step is how a fail-open design stops failing open.
DEFAULT_RERANK_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """One question, asked by one principal inside one knowledge base."""

    query: str
    tenant_id: str
    principal_id: str
    knowledge_base_id: str
    top_k: int = 8


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    """A context packet, plus what its authorization rested on.

    The revisions are kept beside the packet rather than inside it because they
    are not evidence for the reader -- they are what the second check compares
    against, and a ``ContextPacket`` travels to places that have no business
    knowing them.
    """

    packet: ContextPacket
    authorized_revisions: tuple[tuple[str, int], ...]
    #: The cross-encoder's relevance score for the best surviving candidate,
    #: or ``None`` when no reranker ran.
    #:
    #: Kept because it is the only calibrated relevance signal this system
    #: produces, and it used to be discarded: ``_rerank`` scored every passage,
    #: sorted by those scores, and returned the candidates carrying their
    #: *retrieval* scores. Retrieval scores cannot answer "is this relevant" --
    #: an RRF score is a rank sum, so the top hit of an unrelated query still
    #: scores near the maximum. Anything that needs to decide whether the
    #: corpus actually covers a question has to read this instead.
    #:
    #: ``None`` is not "irrelevant". It means nothing measured relevance, and a
    #: caller that treats absence as a low score is guessing; ADR-018's routed
    #: shape refuses to assemble without a reranker for exactly that reason.
    top_relevance: float | None = None
    # Whether a reranker actually produced this order. False both when none is
    # configured and when one failed open, because an ablation that cannot tell
    # those apart from a successful rerank will report a fail-open run as
    # "rerank made no difference" -- a null result manufactured by a timeout.
    reranked: bool = False


class SourcesChangedError(RuntimeError):
    """A source moved between building the context and committing the answer."""


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Retrieval, authorized against PostgreSQL on the way in and out.

    Which retriever proposes the candidates is somebody else's decision -- see
    ``CandidateRetrieverPort``. What happens to them afterwards is this class's
    entire reason to exist, and it does not vary by retriever: authorize, then
    rerank, then cut, then build. ADR-017 lets a framework own the first step;
    it does not let one own any of the rest, because every step after this one
    is a way for text to reach somebody.

    The retriever's own name is reported rather than hidden: ``mode`` says which
    one ran, so an evaluation report cannot label a dense run as hybrid, and an
    ablation comparing two retrievers cannot accidentally compare one to itself.
    """

    candidate_retriever: CandidateRetrieverPort
    documents: DocumentStore
    # Absent when the process has no reranking runtime. Present, it reorders
    # what PostgreSQL already authorized -- never what the index returned.
    reranker: RerankerPort | None = None
    rerank_timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS
    candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER
    # The candidate funnel from `[rag.retrieval]` (ADR-097). Both are optional
    # and both mean the same thing when absent: keep the behaviour this class
    # had before the funnel was wired. That is what lets every in-memory double
    # and all seven test construction sites stay untouched, while the two
    # production sites read the configured values.
    #
    # `None` is "the deployment said nothing", not "the deployment said zero".
    # The second is a config error and `validate_candidate_funnel` already
    # refuses it (`ge=1`), so this field never has to represent it.
    #: Ceiling on the candidate pool asked of the retriever. Absent, the
    #: historical heuristic applies: `top_k * candidate_multiplier`.
    fused_top_k: int | None = None
    #: The most a request may be shown. Absent, a request gets what it asked
    #: for. Present, it is the ceiling `docs/configuration.md` §8 has always
    #: described and nothing has ever enforced.
    rerank_top_k: int | None = None
    # Records nothing unless a process supplies a collector.
    telemetry: Telemetry = field(default_factory=NullTelemetry)

    @property
    def mode(self) -> str:
        """Which retriever this is, for a report to name.

        Names what is configured, not what happened on the last call: a
        property cannot know that. Whether a given turn was actually reranked
        is on its ``AuthorizedContext``.
        """

        base = self.candidate_retriever.mode
        return f"{base}+rerank" if self.reranker is not None else base

    async def _candidates(self, request: RetrievalRequest) -> tuple[ScoredChunk, ...]:
        """Ask the retriever for more than will be kept.

        The multiplier is applied here rather than by the retriever because it
        is a property of what happens *next* -- authorization removes some
        candidates and reranking needs more than top_k to choose from. A
        retriever that decided its own candidate budget would be deciding how
        much room the authorization step has to work with, which is a decision
        it has no way to be right about.
        """

        return await self.candidate_retriever.candidates(
            query=request.query,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            knowledge_base_id=request.knowledge_base_id,
            limit=self._candidate_limit(request.top_k),
        )

    def _candidate_limit(self, top_k: int) -> int:
        """How many to ask the index for.

        Configured, the funnel decides and the number no longer moves with the
        request: a deployment that says 40 means 40 whether the caller wanted 3
        passages or 30. Unconfigured, the multiplier heuristic stands.
        """

        if self.fused_top_k is not None:
            return self.fused_top_k
        return top_k * self.candidate_multiplier

    def _shown(self, top_k: int) -> int:
        """How many the caller may actually be shown.

        A request narrows; it does not widen. This is the one line that makes
        `docs/configuration.md` §8 true -- before it, `rerank_top_k` was a
        number nothing read, and the only ceilings a request met were the
        hardcoded `le=50` on the chat route and `MAX_TOP_K` on the search tool.
        Those stay: they bound a malformed request at the transport, which is a
        different job from bounding a well-formed one at the deployment.

        **Why this can never show more than was asked of the index**: the pair
        is safe because `validate_candidate_funnel` refuses a config where
        `rerank_top_k > fused_top_k`, so `shown <= rerank_top_k <= fused_top_k`
        -- the candidate limit. The guarantee lives in the config layer, not
        here, and this class deliberately does not re-derive it: two places
        enforcing one invariant is how they come to disagree. A process that
        sets `fused_top_k` alone is outside that guarantee and simply gets
        fewer results than it asked for, which is the same thing an
        under-populated index does.
        """

        if self.rerank_top_k is None:
            return top_k
        return min(top_k, self.rerank_top_k)

    async def retrieve(self, request: RetrievalRequest) -> AuthorizedContext:
        """Find, authorize, then build. In that order, and not another."""

        if request.top_k < 1:
            raise ValueError("top_k must be positive")

        started = monotonic()
        with self.telemetry.span("rag.retrieve", attributes={"mode": self.mode}):
            return await self._retrieve(request, started)

    async def _retrieve(
        self, request: RetrievalRequest, started: float
    ) -> AuthorizedContext:
        candidates = await self._candidates(request)

        # The whole point of this module. What came back was filtered by a copy
        # of the ACL; what may be read is decided here.
        readable = await self.documents.readable_versions(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            document_ids=tuple({candidate.document_id for candidate in candidates}),
        )
        revisions = {
            document.document_id: document.source_revision for document in readable
        }

        # Readability alone is not enough. A point from an older content or ACL
        # revision may remain in Qdrant while ingestion catches up, and accepting
        # it merely because the document is still readable would expose text the
        # current PostgreSQL snapshot no longer describes. Equality also rejects
        # an impossible "future" point rather than letting the derived store get
        # ahead of its authority.
        authorized = tuple(
            candidate
            for candidate in candidates
            if revisions.get(candidate.document_id) == candidate.source_revision
        )

        # Reranking happens here and not a line earlier or later. Earlier, it
        # would score passages the asker may not read -- the cross-encoder does
        # read them. Later, it would be reordering a list already cut to top_k,
        # which lets it promote within the retriever's choice but never
        # overturn it, and that is not the thing being evaluated.
        ranked, relevance = await self._rerank(request.query, authorized)
        reranked = relevance is not None
        # Computed once and used for both the cut and the score below: two
        # separate `[: request.top_k]` slices are two chances to disagree about
        # what "shown" means, and the score is supposed to describe the cut.
        shown = self._shown(request.top_k)
        selected = ranked[:shown]
        # The best score among what survived the cut, not among everything
        # scored: a highly relevant passage the caller will never be shown
        # should not vouch for the context it is absent from.
        top_relevance = (
            max(relevance[:shown], default=None) if relevance is not None else None
        )

        # Both counts, because the interesting number is the gap: candidates
        # the index proposed against passages this principal may actually read.
        self.telemetry.record(
            RETRIEVAL_DURATION,
            (monotonic() - started) * 1000,
            attributes={"mode": self.mode},
        )
        self.telemetry.record(
            RETRIEVAL_CANDIDATES, len(candidates), attributes={"mode": self.mode}
        )
        self.telemetry.record(
            RETRIEVAL_AUTHORIZED, len(authorized), attributes={"mode": self.mode}
        )
        return AuthorizedContext(
            packet=_packet(selected),
            authorized_revisions=tuple(
                sorted(
                    (document_id, revisions[document_id])
                    for document_id in {candidate.document_id for candidate in selected}
                )
            ),
            reranked=reranked,
            top_relevance=top_relevance,
        )

    async def _rerank(
        self, query: str, authorized: tuple[ScoredChunk, ...]
    ) -> tuple[tuple[ScoredChunk, ...], tuple[float, ...] | None]:
        """Reorder authorized candidates, or return them untouched.

        Returns the scores alongside the order, in the order returned, so a
        caller can read relevance without recomputing it. ``None`` rather than
        an empty tuple when no reranker ran -- empty would be indistinguishable
        from "scored, and everything scored zero".

        Fail-open, deliberately and narrowly. A reranker is a quality
        improvement over an order that is already usable, so a timeout or a
        broken model must not turn a working answer into an error. What it must
        also not do is widen what the asker can see: the fallback is the input
        list, and the success path is a permutation of that same list selected
        by position, so no path through this method can produce a chunk that
        was not authorized above.
        """

        if self.reranker is None or not authorized:
            return authorized, None

        passages = tuple(candidate.text for candidate in authorized)
        try:
            async with asyncio.timeout(self.rerank_timeout_seconds):
                scores = await self.reranker.rerank(query, passages)
        except Exception:
            # Broad on purpose, and it covers the timeout: asyncio.timeout
            # raises TimeoutError, which is an Exception. Every failure of an
            # optional quality step has the same correct response, and
            # enumerating the ways a model runtime can fail would be a list
            # that goes stale silently.
            #
            # CancelledError is deliberately not caught -- it inherits
            # BaseException, so it passes through here. A cancelled request
            # must stay cancelled rather than fail open into finishing work
            # nobody is waiting for.
            return authorized, None

        if len(scores) != len(authorized):
            # An adapter is allowed to be slow or broken; it is not allowed to
            # be misaligned, because misalignment is indistinguishable from a
            # bad ranking once the scores are attached to the wrong passages.
            return authorized, None

        order = sorted(
            range(len(authorized)),
            # Descending by score, ties broken by the retriever's order rather
            # than arbitrarily, so an unhelpful reranker degrades to the input
            # instead of shuffling it.
            key=lambda index: (-scores[index], index),
        )
        return (
            tuple(authorized[index] for index in order),
            tuple(scores[index] for index in order),
        )

    async def confirm_unchanged(
        self, context: AuthorizedContext, *, tenant_id: str, principal_id: str
    ) -> None:
        """Re-check, after the model has answered and before anyone sees it.

        Raises ``SourcesChangedError`` when a source was revoked or moved on.
        The caller refuses the answer or regenerates from whatever is still
        authorized -- what it must not do is deliver an answer built on a
        document this principal can no longer read.

        Comparing revisions rather than merely re-asking "may I read it" is
        what makes this catch a *replaced* ACL: a grant change advances the
        revision exactly as a content change does, so a document that was
        revoked and re-granted between the two checks still fails.
        """

        revisions = tuple(
            AuthorizedRevision(
                document_id=document_id,
                source_revision=source_revision,
            )
            for document_id, source_revision in context.authorized_revisions
        )
        if not await self.revisions_unchanged(
            revisions,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ):
            raise SourcesChangedError(
                "a document this answer was built from is no longer readable "
                "at the revision it was read at"
            )

    async def revisions_unchanged(
        self,
        revisions: tuple[AuthorizedRevision, ...],
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        """Whether a persisted release snapshot is still readable unchanged."""

        expected = {
            revision.document_id: revision.source_revision for revision in revisions
        }
        if not expected:
            return True

        readable = await self.documents.readable_versions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            document_ids=tuple(expected),
        )
        current = {
            document.document_id: document.source_revision for document in readable
        }

        for document_id, revision in expected.items():
            if current.get(document_id) != revision:
                return False
        return True


def _packet(chunks: tuple[ScoredChunk, ...]) -> ContextPacket:
    """Turn authorized candidates into evidence and the citations for it.

    A citation is built for every chunk that survived, and only for those. The
    two lists are produced together here rather than by separate passes,
    because a citation without its chunk is a reference to something the model
    never saw, and a chunk without a citation is evidence the reader cannot
    check.
    """

    context_chunks = tuple(
        ContextChunk(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            tenant_id=chunk.tenant_id,
            text=chunk.text,
            locator=SourceLocator(paragraph=chunk.ordinal, page=chunk.page),
            score=chunk.score,
        )
        for chunk in chunks
    )
    citations = tuple(
        Citation(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            locator=SourceLocator(paragraph=chunk.ordinal, page=chunk.page),
        )
        for chunk in chunks
    )
    return ContextPacket(
        chunks=context_chunks,
        citations=citations,
        token_estimate=sum(len(chunk.text) // 4 for chunk in chunks),
    )


__all__ = [
    "DEFAULT_CANDIDATE_MULTIPLIER",
    "AuthorizedContext",
    "RetrievalRequest",
    "RetrievalService",
    "SourcesChangedError",
]
