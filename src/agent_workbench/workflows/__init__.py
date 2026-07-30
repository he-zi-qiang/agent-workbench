"""Framework-neutral workflow control flow.

The graph adapter owns compilation, checkpoints and scheduling.  It does not
own the answer to "where does this state go next": that answer is ordinary
Python here, so every edge is testable without compiling a graph and the
adapter has a reference to reproduce rather than a definition to invent.
"""

from __future__ import annotations

from agent_workbench.workflows.research_graph import (
    ENTRY_NODE,
    GRAPH_VERSION_V1,
    STATIC_EDGES,
    TERMINAL_NODE,
    EmptyPlanError,
    MissingReviewError,
    ResearchContribution,
    begin_revision,
    fan_in,
    merge_refs,
    quality_gate_failure_reason,
    route_quality_gate,
    route_research,
)

__all__ = [
    "ENTRY_NODE",
    "GRAPH_VERSION_V1",
    "STATIC_EDGES",
    "TERMINAL_NODE",
    "EmptyPlanError",
    "MissingReviewError",
    "ResearchContribution",
    "begin_revision",
    "fan_in",
    "merge_refs",
    "quality_gate_failure_reason",
    "route_quality_gate",
    "route_research",
]
