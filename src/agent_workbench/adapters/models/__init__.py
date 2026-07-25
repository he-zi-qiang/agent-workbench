"""Model adapters.

Each one converts a provider's stream into the neutral ``ModelEvent`` sequence
and nothing else: retries, timeouts and model selection are settings-driven and
applied around the adapter, not invented inside it.
"""

from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn

__all__ = ["FakeModel", "ScriptedTurn"]
