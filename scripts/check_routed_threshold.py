#!/usr/bin/env python
"""Print the cross-encoder scores the routed shape actually decided on.

``chat.routed_relevance_threshold`` is a number in a file that nothing has ever
measured. ``config.default.toml`` says so in as many words -- "uncalibrated,
measure it against your own corpus before trusting it" -- and there is no way
to measure it from the outside, because the score is not in the answer. It is
carried by ``RetrievalRejected``, which is emitted only on the turn that fell
back. So the log shows the scores *below* the line and nothing above it, and
"is 0.3 the right line?" cannot be answered from one run.

Hence two passes, and this script is the client for both:

    pass 1, collect   AW_CHAT__ROUTED_RELEVANCE_THRESHOLD=1e9
                      Every turn is rejected, so every score is recorded --
                      including the on-topic ones, which is the half a normal
                      run can never show.

    pass 2, confirm   the configured threshold, no override
                      On-topic turns should come back grounded and the control
                      should be rejected with a score below the line.

The control question is not optional and defaults to one, because a run where
everything is grounded proves nothing: a gate that never rejects and a gate
that cannot reject look identical from here.

The failure this exists to catch is not a low score. It is ``top_relevance``
coming back ``None`` on every turn, which means the reranker never answered --
it failed open on a timeout -- and ``_is_grounded`` returned False at the None
check without ever comparing anything to the threshold. Measured on this
machine, that is exactly what a 15-second reranker timeout does to an 8-passage
rerank, and the symptom is indistinguishable from a corpus that covers nothing.
So an all-``None`` run exits 1 and says which of the two it was.

Reads the API as any other client would: create a session, ask, then replay the
session's own event stream. Nothing here imports the application.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from time import monotonic
from typing import Any

# Retrieval on a laptop is slow enough that the usual defaults are wrong by an
# order of magnitude: one 8-passage rerank on MPS was measured at ~108s, before
# the model is even called. A timeout here would look exactly like a hang.
ASK_TIMEOUT_SECONDS = 900
EVENTS_TIMEOUT_SECONDS = 120

# The turn is over when one of these lands. `RunCompleted` is the backstop for
# a turn that failed before committing either kind of answer.
TERMINAL_EVENTS = frozenset(
    {"AnswerCommitted", "UngroundedAnswerCommitted", "RunCompleted", "RunFailed"}
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One question, and whether its corpus is expected to answer it."""

    question: str
    #: False for the control. Named for what it asserts rather than for what it
    #: is, because the whole point of the control is that it is asked exactly
    #: the same way and only the expectation differs.
    expect_grounded: bool


@dataclass(frozen=True, slots=True)
class Result:
    probe: Probe
    grounded: bool
    #: None on a grounded turn, because no rejection was recorded -- which is
    #: not the same as None *on a rejected turn*, where it means the reranker
    #: did not answer. Kept apart everywhere below.
    top_relevance: float | None
    threshold: float | None
    chunk_count: int | None
    rejected: bool
    seconds: float
    answer: str
    #: Set when the turn returned an error instead of an answer. The score is
    #: still read and still reported: the gate runs and emits its event before
    #: the answer is attempted, so a turn that failed *downstream of the
    #: rejection* measured the thing this script exists to measure. Losing that
    #: to an exception was costing a full pass per failure.
    failure: str | None = None


def _request(
    url: str,
    *,
    tenant_id: str,
    principal_id: str,
    scopes: tuple[str, ...] = (),
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> urllib.request.addinfourl:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("x-tenant-id", tenant_id)
    request.add_header("x-principal-id", principal_id)
    if scopes:
        # Sent only when asked for. A principal with no scopes is the honest
        # default here, and it is also what exposed the fallback defect this
        # flag exists to work around: with `research.enabled`, an ungrounded
        # turn offers `web_search` without checking that the caller may use it,
        # so the model proposes it, policy denies it, and the turn burns its
        # step ceiling and 502s instead of just answering without evidence.
        request.add_header("x-principal-scopes", ",".join(scopes))
    if payload is not None:
        request.add_header("content-type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return urllib.request.urlopen(request, timeout=timeout)


def _json(response: urllib.request.addinfourl) -> Any:
    return json.loads(response.read().decode())


def _rejection(
    api_url: str,
    session_id: str,
    *,
    tenant_id: str,
    principal_id: str,
    scopes: tuple[str, ...],
) -> dict[str, Any] | None:
    """The session's ``RetrievalRejected`` payload, or None if it was grounded.

    The stream is a replay that has not finished yet: everything already
    written arrives immediately, and then it stays open forever. So this reads
    until the turn's terminal event and stops, rather than waiting for an end
    that does not come. One session per question upstream is what makes that
    unambiguous -- the only run on this stream is the one being measured.
    """

    response = _request(
        f"{api_url}/v1/chat/sessions/{session_id}/events",
        tenant_id=tenant_id,
        principal_id=principal_id,
        headers={"accept": "text/event-stream"},
        timeout=EVENTS_TIMEOUT_SECONDS,
    )
    rejection: dict[str, Any] | None = None
    with response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            envelope = json.loads(line[len("data:") :].strip())
            if envelope.get("event_type") == "RetrievalRejected":
                rejection = envelope["payload"]
            if envelope.get("event_type") in TERMINAL_EVENTS:
                break
    return rejection


def _ask(
    api_url: str,
    probe: Probe,
    *,
    tenant_id: str,
    principal_id: str,
    scopes: tuple[str, ...],
    knowledge_base_id: str,
    top_k: int,
) -> Result:
    session_id = _json(
        _request(
            f"{api_url}/v1/chat/sessions",
            tenant_id=tenant_id,
            principal_id=principal_id,
            scopes=scopes,
            method="POST",
            body={"title": "threshold check"},
            timeout=30,
        )
    )["session_id"]

    started = monotonic()
    answer: dict[str, Any] = {}
    failure: str | None = None
    try:
        answer = _json(
            _request(
                f"{api_url}/v1/chat/sessions/{session_id}/messages",
                tenant_id=tenant_id,
                principal_id=principal_id,
                method="POST",
                body={
                    "question": probe.question,
                    "knowledge_base_id": knowledge_base_id,
                    "top_k": top_k,
                    # Explicit, though naming a knowledge base would infer it.
                    # The routed shape decides grounded-or-not *inside* a `rag`
                    # turn -- `direct` is a different route that never
                    # retrieves, so asking for it would measure nothing.
                    "answer_mode": "rag",
                },
                headers={"Idempotency-Key": uuid.uuid4().hex},
                timeout=ASK_TIMEOUT_SECONDS,
            )
        )
    except urllib.error.HTTPError as error:
        # Not fatal, and the distinction matters. The turn is one probe of
        # several, the gate already ran, and the failure is usually downstream
        # of it -- a 502 from the fallback answer says nothing about the score
        # the fallback was chosen by. Recorded and carried into the table.
        detail = error.read().decode()
        try:
            body = json.loads(detail)
            detail = str(body.get("detail", detail))
            if isinstance(body.get("error"), dict):
                detail = f"{detail} ({body['error'].get('code')})"
        except (ValueError, AttributeError):
            pass
        failure = f"HTTP {error.code}: {detail[:160]}"
    seconds = monotonic() - started

    rejection = _rejection(
        api_url,
        session_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        scopes=scopes,
    )
    return Result(
        probe=probe,
        grounded=bool(answer.get("grounded")),
        top_relevance=None if rejection is None else rejection.get("top_relevance"),
        threshold=None if rejection is None else rejection.get("threshold"),
        chunk_count=None if rejection is None else rejection.get("chunk_count"),
        rejected=rejection is not None,
        seconds=seconds,
        answer=(answer.get("answer") or "").strip(),
        failure=failure,
    )


def _format_score(result: Result) -> str:
    if not result.rejected:
        return "-- (grounded, no rejection recorded)"
    if result.top_relevance is None:
        return "None  <- reranker did not answer"
    return f"{result.top_relevance:.6g}"


def _report(results: list[Result]) -> int:
    """Print the table, then say whether the run demonstrated anything.

    The exit code is about the *run*, not about the threshold: 0 means the two
    groups separated as expected, 1 means this run cannot support a conclusion
    -- either because no score was measured at all, or because the gate did not
    behave as the pass was set up to make it behave.
    """

    print()
    header = (
        f"{'expect':<9} {'grounded':<9} {'score':<38} "
        f"{'chunks':<7} {'secs':<7} question"
    )
    print(header)
    print("-" * 110)
    for result in results:
        expected = "grounded" if result.probe.expect_grounded else "control"
        print(
            f"{expected:<9} {result.grounded!s:<9} {_format_score(result):<38} "
            f"{result.chunk_count if result.chunk_count is not None else '-'!s:<7} "
            f"{result.seconds:<7.0f} {result.probe.question}"
        )
        if result.failure is not None:
            print(f"{'':<9} {'':<9} {result.failure}")

    thresholds = {r.threshold for r in results if r.threshold is not None}
    if thresholds:
        print()
        print(f"threshold in force: {', '.join(f'{t:g}' for t in sorted(thresholds))}")

    rejected = [r for r in results if r.rejected]
    scored = [r for r in rejected if r.top_relevance is not None]

    print()
    if rejected and not scored:
        print(
            "FAIL: every rejected turn recorded top_relevance = None. That is not\n"
            "      a low score -- it is the reranker never answering, so the\n"
            "      grounded check returned False at the None guard and never\n"
            "      compared anything to the threshold. Raise\n"
            "      rag.reranker.timeout_seconds and run this again; nothing here\n"
            "      says anything about the threshold yet."
        )
        return 1

    if not rejected:
        print(
            "FAIL: nothing was rejected, so no score was recorded and this run\n"
            "      measured nothing. If this was the collect pass, the threshold\n"
            "      override did not reach the server."
        )
        return 1

    grounded_controls = [
        r for r in results if not r.probe.expect_grounded and r.grounded
    ]
    if grounded_controls:
        print(
            "FAIL: the control was answered from the corpus. The threshold is\n"
            "      below the off-topic score, so this gate rejects nothing."
        )
        return 1

    missed = [r for r in results if r.probe.expect_grounded and not r.grounded]
    if missed and any(r.top_relevance is not None for r in missed):
        print(
            "note: an on-topic question fell back. Expected on the collect pass\n"
            "      (that is what it is for); on the confirm pass it means the\n"
            "      threshold is above the on-topic scores below."
        )

    failed = [r for r in results if r.failure is not None]
    if failed:
        print(
            f"note: {len(failed)} turn(s) returned an error *after* the gate had\n"
            "      already run and recorded its score. The scores above stand;\n"
            "      the errors are a separate defect on the fallback path."
        )

    print(f"OK: {len(scored)} score(s) recorded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_routed_threshold",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant-id", default="tenant_local")
    parser.add_argument("--principal-id", default="user_local")
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help=(
            "a permission scope to present, repeatable. Only needed with "
            "research.enabled: pass external:search so the fallback's web tool "
            "is permitted rather than proposed-and-denied until the run dies."
        ),
    )
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        required=True,
        help="a question the corpus should answer; repeatable",
    )
    parser.add_argument(
        "--control",
        action="append",
        default=[],
        help=(
            "a question the corpus must not answer; repeatable. One is supplied "
            "if none is given, because a run with no control cannot tell a gate "
            "that rejects correctly from one that cannot reject at all."
        ),
    )
    arguments = parser.parse_args(argv)

    controls = arguments.control or ["快速排序的平均时间复杂度是多少"]
    probes = [Probe(question=q, expect_grounded=True) for q in arguments.question]
    probes += [Probe(question=q, expect_grounded=False) for q in controls]

    results: list[Result] = []
    for index, probe in enumerate(probes, start=1):
        kind = "grounded" if probe.expect_grounded else "control "
        print(f"[{index}/{len(probes)}] {kind}  {probe.question}", flush=True)
        try:
            results.append(
                _ask(
                    arguments.api_url,
                    probe,
                    tenant_id=arguments.tenant_id,
                    principal_id=arguments.principal_id,
                    scopes=tuple(arguments.scope),
                    knowledge_base_id=arguments.knowledge_base_id,
                    top_k=arguments.top_k,
                )
            )
        except urllib.error.HTTPError as error:
            print(
                f"  HTTP {error.code}: {error.read().decode()[:400]}", file=sys.stderr
            )
            return 1
        except OSError as error:
            print(f"  could not reach {arguments.api_url}: {error}", file=sys.stderr)
            return 1
        print(
            f"      -> {_format_score(results[-1])}  ({results[-1].seconds:.0f}s)",
            flush=True,
        )

    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
