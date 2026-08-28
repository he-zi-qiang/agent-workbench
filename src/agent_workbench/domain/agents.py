"""Who a run may delegate to, and how much of itself it may hand over.

This module is the second meaning of "sub-agent" in this codebase, and the two
are worth telling apart before reading anything below.

The first meaning is ``workflows/agent_profiles.py``: a **node on the compiled
graph**. Which of them exist is decided when the graph is written, their number
is asserted against ``multi_agent.static_agent_node_limit`` at assembly, and a
Task's authorization envelope is frozen knowing exactly which of them can run.

The second meaning is this one: **another run, started from inside a run that
was already going**. Nothing about it is knowable at submission -- not how many,
not which, not whether any at all -- because the model decides mid-loop. That is
the whole point of it and also the whole cost, and every type here exists to put
a ceiling on the part that cannot be known in advance.

Three of those ceilings are written into the return values of two functions
rather than checked by a caller:

* :func:`permitted_child_tools` is an **intersection**, so a child cannot reach
  a tool its parent was not itself allowed. It is the same rule
  ``agent_profiles.permitted_tools`` writes for graph nodes, restated here
  because ``adapters/`` may not import ``workflows/``
  (``tests/architecture/test_dependency_boundaries.py``) -- and restated as a
  function over two lists rather than shared through a base class, because the
  thing worth sharing is the direction, not the code.
* The same function **removes the delegation tools themselves** once the depth
  ceiling is reached. Recursion therefore stops because the grandchild is never
  shown the tool that would spawn it, not because a counter somewhere was
  incremented correctly. A counter is a thing to trust; an absent tool is a
  thing to read.
* :func:`child_envelope` can only ever lower the risk ceiling. There is no
  argument that raises it, which is what makes "a delegation cannot be used to
  escape the envelope the submitter signed" a property of the type rather than
  of every call site remembering.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Annotated, Final

from pydantic import StringConstraints

from agent_workbench.domain.policies import (
    RISK_ORDER,
    AuthorizationEnvelope,
)
from agent_workbench.domain.runs import ModelProfileName
from agent_workbench.domain.tools import ToolName, ToolRisk
from agent_workbench.domain.workspace import (
    WORKSPACE_EDIT_TOOL,
    WORKSPACE_GREP_TOOL,
    WORKSPACE_LIST_TOOL,
    WORKSPACE_READ_TOOL,
    WORKSPACE_WRITE_TOOL,
)

#: The working-set tools, which a sub-agent definition may not name.
#:
#: All five, not only the two that write. Three of them declare ``risk="read"``,
#: so the envelope's risk ceiling does not exclude them and the intersection
#: would hand them over -- which is precisely why this has to be its own rule
#: rather than a consequence of one.
WORKSPACE_TOOL_NAMES: Final[frozenset[ToolName]] = frozenset(
    {
        WORKSPACE_EDIT_TOOL,
        WORKSPACE_GREP_TOOL,
        WORKSPACE_LIST_TOOL,
        WORKSPACE_READ_TOOL,
        WORKSPACE_WRITE_TOOL,
    }
)

#: The tool a run calls to start another run.
#:
#: Declared here rather than beside its handler for the same reason
#: ``domain/workspace.py`` declares ``WORKSPACE_READ_TOOL``: the name is what
#: :func:`permitted_child_tools` has to remove, and the domain cannot import an
#: adapter to ask what it is called.
DELEGATE_TOOL: Final[ToolName] = "delegate_agent"

#: Every tool whose effect is "start another run".
#:
#: A frozenset of one today. It is a set rather than a constant so that the
#: writing counterpart planned for the next tier is added in one place, and so
#: that the removal rule below never has to grow an ``or``.
DELEGATION_TOOL_NAMES: Final[frozenset[ToolName]] = frozenset({DELEGATE_TOOL})

#: Names a model may write to choose a sub-agent. The same shape as
#: ``ToolName``: these arrive from a model, are matched against a catalogue,
#: and end up in an event payload.
SubAgentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

#: How much of a sub-agent's report may travel back inline.
#:
#: The parent's context ceiling is checked *after* a turn, not before it
#: (``agent_runtime`` writes ``last_input_tokens`` when the turn ends and reads
#: it at the top of the next one), so a report large enough to matter is
#: discovered one turn too late. A tool that knows how big its own answer is
#: does not need to be told, and this is that budget.
#:
#: Well below ``ToolOutputText``'s 65,536: that one is the backstop for a tool
#: with no budget of its own, and a sub-agent's answer is prose written for a
#: model to read, not a corpus.
DEFAULT_MAX_REPORT_CHARS: Final[int] = 8_000


@dataclass(frozen=True, slots=True)
class SubAgentDefinition:
    """One kind of run this deployment is willing to start from inside another.

    A sibling of ``workflows.agent_profiles.AgentProfile`` rather than a reuse
    of it, and the difference is not stylistic. ``AgentProfile.node`` is a
    ``TaskNodeId`` written into checkpoint metadata; a delegated run sits on no
    node, so the field would have to be filled with a lie or made optional for
    everybody. And ``adapters/`` -- where the delegation handler has to live,
    because it holds an ``AgentExecutor`` -- may not import ``workflows/`` at
    all.

    What is deliberately *absent* is as load-bearing as what is here. There is
    no budget, no depth, no parent: those describe the delegation, not the
    agent, and a definition that carried them would be a definition that reads
    differently depending on who invoked it.
    """

    name: SubAgentName
    #: What the parent model reads when choosing. It goes into the delegation
    #: tool's own description, so it is written for a model rather than for a
    #: catalogue page.
    description: str
    system_prompt: str
    #: This definition's own ceiling, intersected with the parent's envelope by
    #: :func:`permitted_child_tools`. Naming a tool here grants nothing.
    tool_names: tuple[ToolName, ...] = ()
    #: Which profile the child's model calls resolve through. A profile name,
    #: never a model id -- the same rule ``AgentRunRequest`` states, for the
    #: same reason: a definition must not be able to select an unreviewed model.
    model_profile: ModelProfileName = "main"
    max_report_chars: int = DEFAULT_MAX_REPORT_CHARS

    def __post_init__(self) -> None:
        # Refused at definition time, because the two ways it goes wrong are
        # both silent.
        #
        # A working set is a session pinned to the version *one invocation*
        # read (ADR-028), and whether an invocation opens one is decided from
        # its node's **static** profile (`task_handlers._uses_workspace`). A
        # child holding these tools inside a node whose profile does not name
        # them would be advertised them and then fail every call with
        # `WorkspaceUnavailableError` -- while its run reported success. That
        # is ADR-028 §3.2's failure mode exactly, and a delegated run is the
        # first thing able to reach it.
        #
        # And where the node *did* open one, two children started in the same
        # turn would share it: both writing against a version neither of them
        # read. The version pinning that makes a replay produce another version
        # rather than a second effect is per-invocation, and a delegation is a
        # different invocation.
        #
        # Reachable another way and deliberately left so: nothing stops a
        # *node's own* profile from holding these. That path enters the session
        # first, and it is one invocation.
        forbidden = sorted(set(self.tool_names) & WORKSPACE_TOOL_NAMES)
        if forbidden:
            raise ValueError(
                f"sub-agent {self.name!r} names working-set tools "
                f"({', '.join(forbidden)}); a delegated run shares its "
                "parent's session rather than opening one of its own"
            )


@dataclass(frozen=True, slots=True)
class SubAgentCatalogue:
    """The sub-agents one process is willing to start, resolved once.

    Immutable and built at assembly, which is what makes "who could this run
    have delegated to" answerable for an event stream that has already been
    written -- the same property the tool registry has, and for the same
    reason.

    Deliberately **not** discovered from a directory at run time. A definition
    that appears because a file appeared would make "which agents was this run
    allowed to start" a question about which configuration was written last,
    which is exactly what ``ProjectionInput`` and ``DynamicToolSource`` stay
    closed vocabularies to avoid.
    """

    definitions: tuple[SubAgentDefinition, ...] = ()
    _by_name: dict[str, SubAgentDefinition] = field(
        init=False,
        repr=False,
        compare=False,
        default_factory=dict[str, "SubAgentDefinition"],
    )

    def __post_init__(self) -> None:
        by_name: dict[str, SubAgentDefinition] = {}
        for definition in self.definitions:
            if definition.name in by_name:
                # At assembly rather than at call time. Two definitions under
                # one name is a deployment that cannot say which one a model
                # asking for that name would get, and discovering it on the
                # first delegation means discovering it in production.
                raise ValueError(
                    f"two sub-agent definitions share the name {definition.name!r}"
                )
            by_name[definition.name] = definition
        object.__setattr__(self, "_by_name", by_name)

    def get(self, name: str) -> SubAgentDefinition | None:
        """Return the definition, or ``None`` for a name nobody registered.

        ``None`` rather than an exception, for the same reason ``ToolRegistry``
        answers ``None`` for an unknown tool: a model proposed this name, and
        the proposal still has to become exactly one ``ToolResult`` explaining
        the refusal.
        """

        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        """Registered names in a stable order, for the tool's own description."""

        return tuple(definition.name for definition in self.definitions)

    def narrowed_to(self, registered: Collection[ToolName]) -> SubAgentCatalogue:
        """Keep only what this process can actually run.

        Two narrowings, and both exist because a definition is written against
        the tools the *project* has while a catalogue is offered by one
        assembled *process*.

        **Tools this process did not register are dropped from a definition.**
        ``permitted_child_tools`` intersects with the parent's envelope, and an
        envelope is frozen from configuration -- so it can name a tool this
        process failed to assemble. ``ToolGateway.advertise`` raises for a
        requested tool the registry does not hold, which would make the child
        run fail before its first turn. This is the same rule
        ``profile_with_dynamic_tools`` states for graph nodes, and it is here
        for the same reason.

        **A definition left with none of the tools it named is dropped
        entirely.** ``analyst`` declares no tools and is unaffected; a
        ``researcher`` whose search tool is absent is not a researcher, and
        offering it anyway would put a description in front of the model --
        "answers from the knowledge base" -- that the run behind it cannot
        honour. The model would spend a delegation to be told the agent has
        nothing to work with.
        """

        available = set(registered)
        kept: list[SubAgentDefinition] = []
        for definition in self.definitions:
            if not definition.tool_names:
                kept.append(definition)
                continue
            granted = tuple(name for name in definition.tool_names if name in available)
            if granted:
                kept.append(replace(definition, tool_names=granted))
        return SubAgentCatalogue(tuple(kept))


def permitted_child_tools(
    definition: SubAgentDefinition,
    allowed_by_parent: Sequence[ToolName],
    *,
    child_depth: int,
    max_depth: int,
) -> tuple[ToolName, ...]:
    """The intersection, minus the tools that would deepen the tree.

    Two rules, and neither has a path that widens:

    **Intersection.** A definition's ``tool_names`` is a ceiling of its own, not
    a grant. There is deliberately no argument that returns a tool the parent
    was not itself allowed -- a child with more authority than the run that sent
    it is the failure this function exists to make unwritable.

    **Depth.** At ``child_depth >= max_depth`` the delegation tools come off the
    list entirely, rather than staying on it to be refused when called. The
    difference matters twice. A model that can see ``delegate_agent`` proposes
    ``delegate_agent``, and the only thing left to do with the proposal is turn
    it away -- spending a turn of the child's budget to say no. And a reader
    asking "can this generation spawn another" gets to answer it by comparing
    two lists, instead of by trusting that a counter was threaded correctly
    through every call site.
    """

    allowed = set(allowed_by_parent)
    at_ceiling = child_depth >= max_depth
    return tuple(
        name
        for name in definition.tool_names
        if name in allowed and not (at_ceiling and name in DELEGATION_TOOL_NAMES)
    )


def child_envelope(
    parent: AuthorizationEnvelope,
    definition: SubAgentDefinition,
    *,
    child_depth: int,
    max_depth: int,
    risk_ceiling: ToolRisk = "read",
) -> AuthorizationEnvelope:
    """Narrow the parent's envelope for one delegated run.

    ``risk_ceiling`` is a *further* restriction and never a relaxation: the
    result takes the lower of it and the parent's own ceiling. A definition
    cannot ask for more than the Task's submitter signed, and this signature has
    no argument through which it could.

    ``denied_tools`` and ``approval_required_risks`` travel down unchanged.
    Denial is the half of an envelope that must not be forgotten in a copy, and
    a child that inherited a shorter approval list would be a way to perform,
    one level down, exactly the work a human was meant to be asked about.
    """

    lower: ToolRisk = (
        parent.max_tool_risk
        if RISK_ORDER.index(parent.max_tool_risk) <= RISK_ORDER.index(risk_ceiling)
        else risk_ceiling
    )
    return AuthorizationEnvelope(
        allowed_tools=permitted_child_tools(
            definition,
            parent.allowed_tools,
            child_depth=child_depth,
            max_depth=max_depth,
        ),
        denied_tools=parent.denied_tools,
        max_tool_risk=lower,
        approval_required_risks=parent.approval_required_risks,
    )


__all__ = [
    "DEFAULT_MAX_REPORT_CHARS",
    "DELEGATE_TOOL",
    "DELEGATION_TOOL_NAMES",
    "SubAgentCatalogue",
    "SubAgentDefinition",
    "SubAgentName",
    "child_envelope",
    "permitted_child_tools",
]
