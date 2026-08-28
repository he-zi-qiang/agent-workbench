"""The sub-agents this project ships, and why the list is this short.

Two, and both of them read-only. That is not a placeholder awaiting a fuller
roster: it is what the first tier of ADR-082 is willing to defend.

A delegated run inherits its parent's principal and a *narrowing* of its
envelope, and it is decided on by a model mid-loop rather than by a submitter.
Every property that makes that safe today comes from the child being unable to
change anything: nothing needs approval, so a parent does not sit in
``executing_tools`` waiting on a human it cannot see; nothing is ledgered, so no
effect can be recorded twice by a replay; nothing touches a working set, so the
child does not have to be inside a ``WorkspaceSession`` its parent's node may
not have entered.

Why these two in particular:

``researcher`` is the case delegation exists for. A question that needs eight
searches to answer produces eight tool results, and every one of them stays in
the asking run's context for the rest of its life -- ``compaction`` (ADR-081)
exists because of exactly this. Delegating puts the searching in a run that ends
when the answer is found, and returns the answer.

``analyst`` holds no tools at all, which sounds like a null case and is the
opposite. It is a second opinion that has not read the conversation: it sees the
brief its parent chose to write and nothing else, so it cannot agree with the
parent by inheriting the parent's framing. It is also the cheapest possible
proof that the machinery works, because there is nothing in it but a run.

**Constants, not a directory.** Which sub-agents exist is decided here and
frozen when the process starts, the same way ``AGENT_ROSTERS`` is. A definition
that appeared because a file appeared would make "which agents was this run
allowed to start" a question about which configuration was written last, and
that question has to stay answerable for a stream that was written months ago.
"""

from __future__ import annotations

from typing import Final

from agent_workbench.domain.agents import SubAgentCatalogue, SubAgentDefinition

#: Reads the authorized knowledge base and reports what it found.
#:
#: ``knowledge_search`` is named as this definition's own ceiling. Whether the
#: child actually gets it is decided by intersection with the parent's
#: envelope, so a Task submitted without search authority delegates a
#: ``researcher`` that can search nothing -- and says so, rather than being
#: refused a delegation it was allowed to make.
RESEARCHER: Final = SubAgentDefinition(
    name="researcher",
    description=(
        "Answers one focused question from the authorized knowledge base. "
        "Give it the question and the context it needs; it returns findings "
        "with the chunk ids they rest on."
    ),
    system_prompt=(
        "Answer the question you were given from the authorized knowledge "
        "base, and from nothing else. "
        # Same reason as `ANALYST` below: the clip takes the end.
        "Lead with the answer itself, then the evidence for it -- your report "
        "is cut off at a length you cannot see. "
        "Search as many times as the question "
        "needs. Report only what the retrieved passages support, and cite the "
        "chunk id behind every claim. If the passages do not answer the "
        "question, say which part is unsupported rather than filling it in -- "
        "the run that delegated this is going to build on your answer, and it "
        "cannot tell a grounded sentence from a plausible one."
    ),
    tool_names=("knowledge_search",),
)

#: Reasons over the brief it is handed, and holds no tools whatsoever.
ANALYST: Final = SubAgentDefinition(
    name="analyst",
    description=(
        "Thinks through a self-contained problem and returns its reasoning. "
        "Holds no tools: give it every fact it needs in the prompt. Useful for "
        "a second reading that has not seen this conversation."
    ),
    system_prompt=(
        # Conclusion first, and the order is the whole point. A report is
        # clipped to `max_report_chars` from the end (`clip_report`), so
        # whatever a sub-agent leaves for last is what the parent never sees.
        # Measured 2026-08-28: three analysts each returned a complete
        # enumeration of failure modes and each was cut at 8,000 characters
        # exactly where its 总体结论 section began. The parent then spent three
        # more delegations asking for the conclusions alone and ran into
        # `max_children_per_run`, so one truncation turned one round of
        # delegation into two and then blocked the second.
        "Open with your conclusion in a few sentences, before any reasoning: "
        "your report is cut off at a length you cannot see, and anything you "
        "leave until the end is what gets lost. "
        "Then work through the problem and give the reasoning that supports "
        "that conclusion. You have no tools and no access "
        "to the conversation that sent you: everything you know is in the "
        "brief above. If the brief is missing something the problem needs, say "
        "which fact is missing instead of assuming one -- an assumption you "
        "make here is invisible to whoever reads your answer."
    ),
)

#: What a process delegates to when nothing narrower was assembled.
DEFAULT_SUB_AGENTS: Final = SubAgentCatalogue((RESEARCHER, ANALYST))

__all__ = ["ANALYST", "DEFAULT_SUB_AGENTS", "RESEARCHER"]
