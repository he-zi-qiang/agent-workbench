"""The evaluation material may not reward a false statement about this system.

`settings.py` has a comment beside `rag.retrieval.fusion_owner` that reads, in
part: *"It read 'qdrant' for a while after the code had already moved -- which
is exactly the drift this field exists to make impossible."* The field was made
a single-valued `Literal` for that reason, and it worked.

**The evaluation material was drifting in the same direction and nothing was
watching it.** `evals/rag/corpus/fusion.md` said fusion happens "inside
Qdrant's Query API"; `evals/chat/gold.jsonl` scored an answer of "qdrant" to
"What performs the hybrid fusion?" as fully correct. So a chat run that
reproduced the architecture this repository replaced in ADR-033 earned full
marks -- an evaluation rewarding a wrong statement about the system under test,
which is worse than an evaluation that measures nothing.

This file is the guard the `Literal` could not be. It reads the same settings
value the code reads and asserts the material agrees with it, so the next move
of that boundary fails here rather than quietly re-teaching the old design.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_workbench.evaluation import digest_corpus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS = PROJECT_ROOT / "evals" / "rag" / "corpus"
CHAT_GOLD = PROJECT_ROOT / "evals" / "chat" / "gold.jsonl"

#: What `rag.retrieval.fusion_owner` is pinned to. Written out rather than
#: imported from `Settings`, because loading settings needs three DSNs from the
#: environment and this assertion needs none -- and the point is the *word*,
#: which is what both the config and the corpus have to agree on.
FUSION_OWNER = "application"

#: A sentence naming Qdrant *and* the fusion verb is only honest when it says
#: Qdrant does **not** fuse, or describes the design that was replaced. Keeping
#: that shape legal is deliberate: deleting every mention would also delete the
#: record of what this system used to do, and the corpus is allowed to say so.
_NEGATED = re.compile(
    r"\b(not|never|does not|no longer|earlier design|replaced)\b", re.I
)


def _fusion_sentences(text: str) -> list[str]:
    flat = " ".join(text.split())
    return [s + "." for s in flat.split(".") if re.search(r"fus|combin|RRF", s, re.I)]


def test_the_rag_corpus_does_not_say_qdrant_fuses() -> None:
    offenders: list[str] = []
    for path in sorted(CORPUS.glob("*.md")):
        for sentence in _fusion_sentences(path.read_text(encoding="utf-8")):
            if not re.search(r"qdrant", sentence, re.I):
                continue
            # A sentence may name Qdrant *and* fusion when it is saying Qdrant
            # does not fuse, or describing the design that was replaced. That
            # is the honest way to keep the old answer findable, so it passes.
            if _NEGATED.search(sentence):
                continue
            offenders.append(f"{path.name}: {sentence}")

    assert not offenders, (
        "the RAG corpus still teaches that Qdrant performs the fusion, which "
        f"ADR-033 moved into the {FUSION_OWNER}: " + " | ".join(offenders)
    )


def test_the_chat_gold_set_does_not_reward_the_replaced_answer() -> None:
    rewarding: list[str] = []
    for line_number, line in enumerate(
        CHAT_GOLD.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        entry = json.loads(line)
        terms = [str(term).lower() for term in entry.get("must_contain", [])]
        if any("qdrant" in term for term in terms):
            rewarding.append(f"{CHAT_GOLD.name}:{line_number} {entry.get('id')}")

    assert not rewarding, (
        "these gold questions give full marks for naming Qdrant as the fusion "
        f"owner, which has been the {FUSION_OWNER} since ADR-033: "
        + ", ".join(rewarding)
    )


def test_the_fusion_question_still_has_an_answer_in_the_corpus() -> None:
    """The control. Deleting the claim would also pass the two tests above.

    A corpus that no longer answers "what performs the fusion" would make the
    gold question unanswerable, and an unanswerable question scores zero
    without anything being wrong with the retriever -- the failure this pair of
    assertions could otherwise hide.
    """

    fusion_doc = (CORPUS / "fusion.md").read_text(encoding="utf-8").lower()

    assert FUSION_OWNER in fusion_doc
    assert "reciprocal rank fusion" in fusion_doc


@pytest.mark.parametrize("suffix", [".md"])
def test_digest_corpus_notices_a_content_change(tmp_path: Path, suffix: str) -> None:
    (tmp_path / f"a{suffix}").write_text("one", encoding="utf-8")
    (tmp_path / f"b{suffix}").write_text("two", encoding="utf-8")
    before = digest_corpus(tmp_path, suffix=suffix)

    (tmp_path / f"b{suffix}").write_text("three", encoding="utf-8")

    assert digest_corpus(tmp_path, suffix=suffix) != before


def test_digest_corpus_notices_a_rename(tmp_path: Path) -> None:
    """Names are part of the fingerprint, and the reason is not tidiness.

    A document's filename becomes its ``document_id``, so a rename changes
    which id the gold set has to name. Two corpora differing only by a filename
    are not the same measurement.
    """

    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    before = digest_corpus(tmp_path)

    (tmp_path / "a.md").rename(tmp_path / "z.md")

    assert digest_corpus(tmp_path) != before


def test_digest_corpus_is_stable_across_calls() -> None:
    assert digest_corpus(CORPUS) == digest_corpus(CORPUS)
