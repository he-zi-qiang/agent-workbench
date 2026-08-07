"""Ask a running workbench real questions and read what the routed gate decided.

`routed` answers from retrieved evidence when the cross-encoder scores it high
enough and from the model alone when it does not, and
`chat.routed_relevance_threshold` is where "high enough" is written down. That
number ships uncalibrated on purpose: bge-reranker scores are unbounded logits
whose useful cut depends on the corpus. The only way to know whether the
configured one fits this corpus is to ask real questions of a real index and
read the scores off the turns that fell back.

This drives the HTTP API the way any client would, so what it reports is what
the deployed surface decided rather than what the code would decide. The suite
already covers the latter, and both defects this path has had were deployment
shaped: a gate built on a retrieval score, which made `routed` behave exactly
like `fixed`, and then a reranker timing out under its configured limit, which
made every turn fall back regardless of what the corpus held.

**Scores exist only on turns that fell back.** `RetrievalRejected` carries them;
a grounded turn emits no counterpart, because the score that cleared the bar is
not what an operator lowering the bar needs to see. So a single pass shows the
scores below the line and nothing above it. To see the whole distribution, run a
harvest pass with the bar above every reachable score:

    AW_CHAT__RETRIEVAL_SHAPE=routed AW_CHAT__ROUTED_RELEVANCE_THRESHOLD=1e9 \
      scripts/dev.sh api

Every turn is then rejected and every score recorded, on-topic and control
alike, and a threshold that fits is one that separates the two groups. Restart
at the configured value and run again to confirm that it does.

Pass at least one `--question` that the corpus can answer, and keep the control:
a deliberately off-topic question is what distinguishes a working gate from one
that grounds everything, which is the failure this shape had first.

Exits non-zero when it cannot confirm something -- including the case that
matters most here, every fallback reporting no score at all. That is not a low
score. It is a reranker that never answered, and the threshold is never even
compared on those turns.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

#: The ask is synchronous and pays for retrieval plus a model call. On a laptop
#: loading its own weights that is minutes, not seconds, and a client timeout
#: shorter than the server's request timeout reports a failure the server never
#: had.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: Off-topic under any corpus this project would hold. The control is not
#: decoration: without it, a gate that grounds every question looks identical to
#: one that works.
DEFAULT_CONTROL = "What is a good recipe for sourdough bread?"

TERMINAL_KINDS = frozenset(
    {"AnswerCommitted", "UngroundedAnswerCommitted", "AnswerWithheld"}
)


@dataclass(frozen=True, slots=True)
class Turn:
    """One question, and what the deployment did with it."""

    label: str
    question: str
    grounded: bool
    #: Present only when the turn fell back; `None` on a grounded turn, where no
    #: score is recorded at all.
    chunk_count: int | None
    #: `None` on a grounded turn, and also on a fallback where the reranker did
    #: not answer -- two very different things, distinguished by `grounded`.
    top_relevance: float | None
    threshold: float | None

    @property
    def scored(self) -> bool:
        return self.top_relevance is not None


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {"x-tenant-id": args.tenant_id, "x-principal-id": args.principal_id}


def _step(label: str) -> None:
    print(f"\n\033[1m{label}\033[0m", flush=True)


def _fail(reason: str, hint: str) -> int:
    print(f"\n\033[31mstopped\033[0m {reason}\n  try: {hint}\n", flush=True)
    return 1


def _ask(client: httpx.Client, args: argparse.Namespace, question: str) -> str:
    """Open a session and ask one question in it. Returns the session id.

    One session per question, because the event stream is per session and a
    shared one would make every turn replay its predecessors.
    """

    created = client.post(
        "/v1/chat/sessions",
        headers=_headers(args),
        json={"title": f"threshold check {uuid.uuid4().hex[:8]}"},
    )
    created.raise_for_status()
    session_id = str(created.json()["session_id"])

    answered = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={**_headers(args), "Idempotency-Key": f"thr-{uuid.uuid4().hex}"},
        json={
            "question": question,
            "knowledge_base_id": args.knowledge_base_id,
            "answer_mode": "rag",
            "top_k": args.top_k,
        },
    )
    answered.raise_for_status()
    return session_id


def _rejection(
    client: httpx.Client, args: argparse.Namespace, session_id: str
) -> dict[str, Any] | None:
    """The `RetrievalRejected` payload for this session, or None if grounded.

    The stream replays durable events from the start and then stays open, so
    this reads until the turn's terminal event rather than to end of stream.
    """

    deadline = time.monotonic() + args.timeout
    found: dict[str, Any] | None = None
    with client.stream(
        "GET",
        f"/v1/chat/sessions/{session_id}/events",
        headers=_headers(args),
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if time.monotonic() > deadline:
                break
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[len("data:") :].strip())["payload"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # A heartbeat or a frame shaped otherwise. Skipping is right:
                # this reads the two kinds it names and ignores the rest.
                continue
            kind = payload.get("kind")
            if kind == "RetrievalRejected":
                found = payload
            if kind in TERMINAL_KINDS:
                break
    return found


def _run(
    client: httpx.Client, args: argparse.Namespace, label: str, question: str
) -> Turn:
    session_id = _ask(client, args, question)
    rejected = _rejection(client, args, session_id)
    if rejected is None:
        return Turn(
            label=label,
            question=question,
            grounded=True,
            chunk_count=None,
            top_relevance=None,
            threshold=None,
        )
    relevance = rejected.get("top_relevance")
    threshold = rejected.get("threshold")
    return Turn(
        label=label,
        question=question,
        grounded=False,
        chunk_count=rejected.get("chunk_count"),
        top_relevance=None if relevance is None else float(relevance),
        threshold=None if threshold is None else float(threshold),
    )


def _table(turns: list[Turn]) -> None:
    print(
        f"\n  {'':<9} {'grounded':<9} {'chunks':<7} {'top_relevance':<14} question",
        flush=True,
    )
    for turn in turns:
        chunks = "-" if turn.chunk_count is None else str(turn.chunk_count)
        if turn.grounded:
            score, highlight = "(not recorded)", False
        elif turn.top_relevance is None:
            score, highlight = "null", True
        else:
            score, highlight = f"{turn.top_relevance:.4f}", False
        # Pad first and colour the padded cell: an escape sequence inside a
        # width-limited field is counted as characters and shifts the columns.
        cell = f"{score:<14}"
        if highlight:
            cell = f"\033[31m{cell}\033[0m"
        print(
            f"  {turn.label:<9} {turn.grounded!s:<9} {chunks:<7} {cell} "
            f"{turn.question[:44]}",
            flush=True,
        )


def _read(turns: list[Turn]) -> int:
    """Say what the numbers mean for the configured threshold."""

    fell_back = [turn for turn in turns if not turn.grounded]
    thresholds = {turn.threshold for turn in fell_back if turn.threshold is not None}

    if fell_back and not any(turn.scored for turn in fell_back):
        return _fail(
            "every fallback reported top_relevance=null: the reranker did not "
            "answer, so the threshold was never compared and nothing here says "
            "anything about it",
            "raise rag.reranker.timeout_seconds, and check the API log for the "
            "cross-encoder finishing its load before the first question",
        )

    _step("reading")
    if thresholds:
        for value in sorted(thresholds):
            print(f"  configured threshold      {value}", flush=True)

    asked = [turn for turn in turns if turn.label == "question"]
    controls = [turn for turn in turns if turn.label == "control"]
    scored = [turn for turn in fell_back if turn.scored]
    if scored:
        low = min(turn.top_relevance or 0.0 for turn in scored)
        high = max(turn.top_relevance or 0.0 for turn in scored)
        print(f"  scores seen below the bar {low:.4f} .. {high:.4f}", flush=True)

    verdicts: list[str] = []
    if any(turn.grounded for turn in controls):
        verdicts.append(
            "a control question was answered FROM EVIDENCE. Either the bar is "
            "too low or that question is not off-topic for this corpus -- the "
            "first version of this shape grounded everything, so check which."
        )
    if any(not turn.grounded for turn in asked):
        verdicts.append(
            "an on-topic question fell back. If the corpus does answer it, the "
            "bar is above the score printed for it."
        )
    if (
        asked
        and controls
        and all(t.grounded for t in asked)
        and all(not t.grounded for t in controls)
    ):
        verdicts.append(
            "on-topic grounded, control rejected: the configured threshold "
            "separates them on these questions."
        )
    if not any(turn.grounded for turn in turns):
        verdicts.append(
            "nothing was grounded. Harvest pass, or a bar above every score."
        )

    for verdict in verdicts:
        print(f"\n  {verdict}", flush=True)

    print(
        "\n  Scores above the bar are never recorded. For the whole "
        "distribution,\n  restart the API with "
        "AW_CHAT__ROUTED_RELEVANCE_THRESHOLD=1e9 and run again:\n  every turn "
        "is rejected, so every score is printed.",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_routed_threshold",
        description="Ask real questions and read the routed relevance gate.",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant-id", default="tenant_local")
    parser.add_argument("--principal-id", default="user_local")
    parser.add_argument("--knowledge-base-id", default="kb_local")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--question",
        action="append",
        required=True,
        help="An on-topic question this corpus can answer. Repeatable.",
    )
    parser.add_argument(
        "--control",
        action="append",
        help="A deliberately off-topic question. Defaults to one. Repeatable.",
    )
    args = parser.parse_args(argv)
    controls: list[str] = args.control or [DEFAULT_CONTROL]

    client = httpx.Client(base_url=args.api_url, timeout=args.timeout)
    with client:
        _step("health")
        try:
            ready = client.get("/health/ready")
        except httpx.HTTPError:
            return _fail(
                f"the API is not answering on {args.api_url}",
                "AW_CHAT__RETRIEVAL_SHAPE=routed scripts/dev.sh api",
            )
        print(f"  ready {ready.status_code}", flush=True)

        _step("asking")
        turns: list[Turn] = []
        for label, questions in (("question", args.question), ("control", controls)):
            for question in questions:
                try:
                    turn = _run(client, args, label, question)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 422:
                        return _fail(
                            "the API refused the request: this deployment is "
                            "probably not serving the routed shape",
                            "AW_CHAT__RETRIEVAL_SHAPE=routed scripts/dev.sh api",
                        )
                    return _fail(
                        f"ask failed with HTTP {error.response.status_code}",
                        "check the API log; the request bodies are in it",
                    )
                except httpx.HTTPError as error:
                    return _fail(
                        f"ask did not complete: {error}",
                        f"--timeout is {args.timeout:.0f}s; retrieval plus a "
                        f"model call on a cold cross-encoder can exceed it",
                    )
                turns.append(turn)
                print(f"  asked ({label}) {question[:56]}", flush=True)

        _table(turns)
        return _read(turns)


if __name__ == "__main__":
    sys.exit(main())
