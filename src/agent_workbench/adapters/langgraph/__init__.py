"""LangGraph-backed workflow adapter.

This is the only package allowed to import ``langgraph``.  It compiles the
control-flow declaration that ``agent_workbench.workflows`` owns; it does not
restate any routing decision, so the graph a checkpoint replays and the graph
the unit tests assert on cannot disagree.
"""

from __future__ import annotations

from agent_workbench.adapters.langgraph.approval import (
    ApprovalNodeHandler,
    build_approval_node,
)
from agent_workbench.adapters.langgraph.checkpointer import (
    PostgresCheckpointSaver,
    StaleCheckpointWriteError,
)
from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_DEFINITIONS,
    GraphDefinition,
    LangGraphTaskWorkflow,
    NodeHandler,
    build_v1_graph,
    build_v2_graph,
)

__all__ = [
    "GRAPH_DEFINITIONS",
    "ApprovalNodeHandler",
    "GraphDefinition",
    "LangGraphTaskWorkflow",
    "NodeHandler",
    "PostgresCheckpointSaver",
    "StaleCheckpointWriteError",
    "build_approval_node",
    "build_v1_graph",
    "build_v2_graph",
]
