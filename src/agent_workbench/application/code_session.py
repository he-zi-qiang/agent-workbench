"""A coding session: one conversation, one workspace, no coordination plane.

Code shares an identity with Chat -- a tenant, a principal, an ordered history,
the same two tables -- and shares no lifecycle with it at all. Chat publishes an
answer through a turn ledger: claim, lease, release_pending, an assistant
message that an ``AnswerCommitted`` authorised. Code writes no row in that
ledger. Its product is the files in a workspace and a report about them, and
neither of those is an answer that has to pass a fence.

What follows from that is the whole design, and every part of it is a cost
taken deliberately rather than a feature deferred:

* **One turn per session, held in this process.** A set of session ids, not a
  database row. A durable active-turn slot would need a writer that can release
  it after a crash, and that writer is the lease and the reaper -- exactly the
  machinery being declined. So the slot dies with the process, which is correct,
  because so does the turn.

* **A turn is not recoverable.** No lease to expire, no ``release_pending`` to
  finish, nothing half-written to reclaim. If this process dies mid-turn the
  turn is gone and the workspace stands at its last successful write. The user
  says the sentence again. Recorded as ``docs/known-gaps.md`` F-01.

* **The workspace pointer moves per write, not at the end.** A cancelled turn
  keeps the files it had finished, because those files are what the user was
  building. ``application/session_workspace.py`` argues that at length.

* **No answer may be published.** The sink is a ``ProcessOnlySink``, and it is
  named in the signature rather than in a comment so that handing this service
  an ordinary sink does not type-check.

The concurrency bound and the per-session slot are both fail-fast rather than
queues. A caller that waits for a slot is a caller holding a connection open to
find out it could have been told immediately, and a queue in front of a
minutes-long turn is a queue nobody can reason about the length of.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.application.code_approvals import ApprovalScope
from agent_workbench.application.code_prompt import (
    CODER_SYSTEM_PROMPT,
    CODER_SYSTEM_PROMPT_PROJECT,
    CODER_SYSTEM_PROMPT_WITH_SANDBOX,
    CODER_SYSTEM_PROMPT_WITH_SANDBOX_UNGATED,
    with_host_commands,
    with_plan_only,
    with_write_gate,
)
from agent_workbench.application.file_read_receipts import ReadReceipts
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.application.projects import ProjectService
from agent_workbench.application.session_titles import title_from_instruction
from agent_workbench.application.session_workspace import SessionWorkspace
from agent_workbench.application.workspace import (
    Workspace,
    WorkspaceListing,
    WorkspaceSession,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.messages import Message, assistant_message, user_message
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    PrincipalContext,
    risk_within,
)
from agent_workbench.domain.project_files import PROJECT_RUN_TOOL
from agent_workbench.domain.research import WEB_SEARCH_TOOL
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TraceContext,
)
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from agent_workbench.domain.tools import ToolName, ToolRisk
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.conversation_store import (
    ConversationSession,
    ConversationStore,
    StoredMessage,
)
from agent_workbench.ports.project_files import ProjectFileStore
from agent_workbench.ports.tools import ToolRegistry

#: What a coding session reaches on the flat side when nothing wider was
#: granted: read tools plus the two writes.
#:
#: The sentence that used to follow -- "a deployment that leaves
#: `code.sandbox_enabled` false gets exactly this, and therefore never reaches
#: the approval gate" -- was deleted in two goes, and both halves are worth
#: recording because a property comment that has quietly stopped being true is
#: worse than none.
#:
#: The first half went with ADR-073: `code.sandbox_enabled` decides the *flat*
#: tuple only, and the project side does not read it at all
#: (`apps/api/dependencies.py`, "is not read on this side any more").
#:
#: The second half went with ADR-0085, which gave a session a second `external`
#: tool that is not the sandbox. "Never reaches the approval gate" is now a
#: claim about a specific tuple rather than about a deployment, and it is
#: `test_the_flat_tuple_holds_nothing_that_reaches_the_gate` that keeps it --
#: asserted against the registered `ToolSpec`s, so a tool whose risk changes
#: fails the test rather than the comment.
CODE_TOOLS: tuple[ToolName, ...] = (
    "workspace_edit",
    "workspace_grep",
    "workspace_list",
    "workspace_read",
    "workspace_write",
)

#: What a coding session reaches when its project **is a directory** (ADR-073).
#:
#: Disjoint from `CODE_TOOLS`, and that is the invariant rather than a naming
#: choice. A model holding both sets has two "write a file" tools whose
#: descriptions differ only in a word it cannot verify, and the failure that
#: follows does not raise: `project_write("draft.md", …)` succeeds and puts a
#: scratch file in somebody's repository root.
#:
#: There is a grep here now, and what withheld it is what shapes it. The
#: objection was never the matching -- `grep_workspace` was already a pure
#: function over `(name, text)` pairs -- it was that a real tree has four ways
#: to come back incomplete that a manifest held in memory does not: the walk
#: stops at `MAX_LISTING_ENTRIES`, the read budget runs out, a file is not
#: UTF-8, a file is over `MAX_READ_BYTES`. A model told it can grep stops
#: listing and reading, so any one of those going unsaid is how it concludes a
#: file is not there. `ProjectGrepTool` therefore names every file it did not
#: search, on every reply including "No matches" -- that sentence is the
#: feature, and the matching is the part that was already written.
CODE_PROJECT_TOOLS: tuple[ToolName, ...] = (
    "project_edit",
    "project_grep",
    "project_list",
    "project_read",
    "project_write",
)

#: The same list plus the one external tool that belongs to *this side*
#: (ADR-057). Spelled out as its own tuple rather than assembled at the call
#: site, so "what may a coding session reach" has two answers to read rather
#: than one answer and an append.
#:
#: That argument has a premise, and ADR-0085 is where it was worth stating:
#: spelling combinations out beats appending only while there are fewer names
#: than combinations. `sandbox_run` and `project_run` each belong to one side
#: because of what they are -- one reads a ContextVar the other side never
#: sets, the other needs a directory -- so they add tuples, not dimensions.
#: `web_search` is true of both sides and adds a dimension; written this way it
#: would double the four literals and bring back the `_AND_` names ADR-077 had
#: just deleted. It is therefore a flag on the service and one append, and this
#: sentence is why the two are not treated the same.
#:
#: `sandbox_run` is `external` risk. Whether that stops at a human is
#: `code.external_requires_approval`, which defaults to `false` (ADR-058) --
#: the container is the safety story, not the gate. `destructive` stays armed
#: regardless, which is what `project_run` sits behind.
CODE_TOOLS_WITH_SANDBOX: tuple[ToolName, ...] = (
    *CODE_TOOLS,
    SANDBOX_RUN_TOOL,
)

#: The project-directory set plus the tool that runs a command on this machine
#: (ADR-077).
#:
#: Project-only, and that is a property of the tool rather than a choice made
#: here: `project_run` runs *somewhere*, and the only working directory a
#: coding session has is the project's. A flat-workspace turn has no directory
#: to be in, so there is no `CODE_TOOLS_WITH_RUN` to pair with these -- the
#: absence is the answer, not an omission.
#:
#: There is no sandbox arm here, and there was one. `CODE_PROJECT_TOOLS_WITH_
#: SANDBOX` and `..._WITH_SANDBOX_AND_RUN` offered `sandbox_run` to a turn that
#: could never call it: the tool holds a `WorkspaceScope` and reads its session
#: out of a ContextVar (`adapters/tools/sandbox.py` `_session`), and `run()`
#: enters exactly one scope -- for a project turn, `ProjectFileScope`. So every
#: call raised `SandboxUnavailableError` before reaching the sandbox at all,
#: and under `config.demo-local.toml`, where every session has a project, that
#: was every call. Widening the sandbox to *see* a project directory is a
#: capability change against ADR-029's isolation flags and wants its own ADR;
#: until then the honest offer is not to offer it.
CODE_PROJECT_TOOLS_WITH_RUN: tuple[ToolName, ...] = (
    *CODE_PROJECT_TOOLS,
    PROJECT_RUN_TOOL,
)

#: The project side as a set, for the two questions asked about it below:
#: "is this a project turn" and "did a flat-scoped tool leak into one".
_PROJECT_TOOLS: frozenset[ToolName] = frozenset(CODE_PROJECT_TOOLS)

#: Everything that finds its backing through the flat `WorkspaceScope`.
#:
#: Named so the check below can be written as a claim about scopes rather than
#: as a list of names somebody has to keep in step: what makes `sandbox_run`
#: wrong for a project turn is not that it is external, it is that it reads a
#: ContextVar that a project turn does not set.
_WORKSPACE_SCOPED_TOOLS: frozenset[ToolName] = frozenset(
    (*CODE_TOOLS, SANDBOX_RUN_TOOL)
)


def _assert_project_tuples_enter_their_own_scope() -> None:
    """A turn may not be offered a tool whose scope it does not enter.

    At import, because the tuples are literals and there is nothing to wait
    for: the cost of getting this wrong is not an exception, it is a model
    holding a tool that answers `unhandled SandboxUnavailableError`, obeying
    discipline 3 by not retrying, and reporting that it could not run the code
    -- a turn spent on a refusal nobody chose. The shape is ADR-075's
    `_assert_no_profile_offers_a_ledgered_tool`, moved to import time because
    these tuples are not built from configuration.
    """

    for offered in (CODE_PROJECT_TOOLS, CODE_PROJECT_TOOLS_WITH_RUN):
        leaked = _WORKSPACE_SCOPED_TOOLS & frozenset(offered)
        if leaked:
            raise ValueError(
                "a project turn enters only ProjectFileScope, but these tools "
                f"read the flat workspace scope: {sorted(leaked)}"
            )


_assert_project_tuples_enter_their_own_scope()


def _assert_every_prompt_combination_resolves() -> None:
    """Every world a turn can start in has exactly one prompt (ADR-0085).

    `with_host_commands` and `with_web_search` both work by finding **exactly
    one** claim to correct and raising when they find zero or two. That is the
    right discipline -- a base prompt that drifted would otherwise ship a turn
    holding a tool it has been told it does not have -- but it makes the two
    rewriters' anchor sets a coupled pair, and the coupling is invisible from
    either file.

    It is invisible in a specific and expensive way. `with_host_commands`
    replaces a whole no-shell sentence with `_HAS_SHELL`, and `_HAS_SHELL`
    describes the network as reachable rather than absent -- so after it runs,
    none of the three no-shell spellings is in the prompt any more and the
    fourth anchor is. A `with_web_search` whose anchors were only the first
    three would find zero matches and raise **on every project turn of a
    deployment that granted both**, which is `config.code-local.toml`'s default
    pair. Not at import, not in one test: a 500 per turn, in production, from a
    module that type-checks.

    So the combinations are enumerated here and evaluated at import, where a
    drift costs a failed process start instead of a failed conversation. Four
    tuples x gated/ungated x search/no-search x plan/act is 32 evaluations of a
    pure function over string constants; it costs nothing and it is the only
    place the pair is checked together.

    Deliberately **not** folded into `_assert_project_tuples_enter_their_own_
    scope` above. That one asks whether a tuple leaks a tool into a scope it
    does not enter, and `web_search` enters no scope at all -- it would be
    trivially true there for ever, which is the same as not being checked.
    """

    from agent_workbench.application.code_prompt import with_web_search

    tuples = (
        CODE_TOOLS,
        CODE_TOOLS_WITH_SANDBOX,
        CODE_PROJECT_TOOLS,
        CODE_PROJECT_TOOLS_WITH_RUN,
    )
    for names in tuples:
        for gated in (False, True):
            for plan_only in (False, True):
                base = _system_prompt_for(
                    names,
                    external_requires_approval=gated,
                    plan_only=plan_only,
                )
                # The search arm is applied to the same base the service
                # applies it to, so a rewriter that cannot find its anchor
                # raises here rather than on somebody's turn.
                with_web_search(base)


def _system_prompt_for(
    tool_names: tuple[ToolName, ...],
    *,
    external_requires_approval: bool,
    plan_only: bool = False,
    write_gate: bool = False,
) -> str:
    """What this turn is told about the world it is in.

    Selected from the same facts the envelope reads, so the model is never told
    it cannot do something it has been granted -- nor that it will wait for a
    human who is not going to be asked (ADR-058: the gated text says "expect to
    wait, do not spend one", which under no gate is an instruction to avoid the
    tool the deployment just freed).

    Three axes now, and they still compose rather than multiply: the file
    language picks the base, the sandbox gate picks between the two flat bases,
    and `project_run` is applied on top of whichever was chosen. The project
    side has no sandbox arm because a project turn is no longer offered
    `sandbox_run` at all -- see `CODE_PROJECT_TOOLS_WITH_RUN`.

    `plan_only` is the one fact that is *not* read off `tool_names`, and the
    asymmetry is deliberate: a narrowed list and a list that simply never held
    a write tool are indistinguishable by inspection, and only one of them
    should be told that writing was taken away from it (ADR-0079).

    The file language is read off `tool_names` rather than passed in beside it,
    for the reason `code_risk_ceiling` derives the ceiling the same way: two
    switches are two ways to describe one decision, and the interesting bug is
    the pair disagreeing. Here that bug has a measured shape -- a turn holding
    `project_write` and told that its writes produce a new version of a set
    that does not exist (`docs/known-gaps.md` F-23).
    """

    if _PROJECT_TOOLS & frozenset(tool_names):
        base = CODER_SYSTEM_PROMPT_PROJECT
    elif SANDBOX_RUN_TOOL in tool_names:
        base = (
            CODER_SYSTEM_PROMPT_WITH_SANDBOX
            if external_requires_approval
            else CODER_SYSTEM_PROMPT_WITH_SANDBOX_UNGATED
        )
    else:
        base = CODER_SYSTEM_PROMPT
    if PROJECT_RUN_TOOL in tool_names:
        base = with_host_commands(base)
    if plan_only:
        base = with_plan_only(base)
    # After `with_plan_only`, and only ever without it: a plan turn holds no
    # write tool at all, so telling it that its writes will stop at a person is
    # describing a world it is not in -- the exact failure this whole selection
    # exists to avoid. The two are mutually exclusive at the call site as well,
    # because the ladder has one position per turn; the `and` here is what
    # makes that true of this function on its own.
    if write_gate and not plan_only:
        base = with_write_gate(base)
    return base


def read_only(
    tool_names: tuple[ToolName, ...], *, risks: Mapping[ToolName, ToolRisk]
) -> tuple[ToolName, ...]:
    """The offered tools that only read, by their own ``ToolSpec`` (ADR-0079).

    Narrowed by risk rather than by name, and that distinction is the whole
    reason this is a function instead of a filter on `*_write`/`*_edit`
    spelled at the call site. A name-suffix filter is a second place a tool's
    risk is written down, with a wildcard in it: the first tool that does not
    follow the convention -- a future `project_move`, anything bound from MCP
    -- would slip into a turn that has been told, in its prompt and in its
    envelope, that it cannot change anything.

    Order is preserved so the envelope of a plan turn is a subsequence of the
    act turn's, which is what makes "plan only ever narrows" checkable by
    reading the two lists rather than by trusting this function.
    """

    return tuple(name for name in tool_names if risks.get(name) == "read")


def code_approval_risks(
    approvals: CodeApprovals, *, external_requires_approval: bool
) -> tuple[ToolRisk, ...]:
    """Which risks stop at a person for this turn (ADR-087).

    The deployment's own set is computed first and is always in the answer;
    the session's choice can only add to it. That is the whole safety argument
    of this axis written as code rather than as a sentence: there is no branch
    in which `"destructive"` is absent, so a console cannot ask for a turn that
    runs `project_run` without anybody being asked -- which is the thing
    ADR-077 exists to prevent, and which a `Literal` with a third position
    would have made one enum value away.

    It is the same shape as `read_only` one function up, for the same reason.
    Plan mode's invariant -- "plan only ever narrows" -- is checkable by
    reading two lists rather than by trusting the function that produced them.
    Here it is checkable by reading one expression: `base` is a subsequence of
    every return.

    ``external`` is not this axis's business either way. Whether a search or a
    container stops at a human is ADR-058's and ADR-0085's question and the
    deployment answers it; a session that wanted the write gate has said
    nothing about the network.
    """

    base: tuple[ToolRisk, ...] = (
        ("external", "destructive") if external_requires_approval else ("destructive",)
    )
    if approvals == "standard":
        return base
    return ("write", *base)


def code_risk_ceiling(
    tool_names: tuple[ToolName, ...], *, risks: Mapping[ToolName, ToolRisk]
) -> ToolRisk:
    """The lowest ceiling that admits everything in ``tool_names``.

    Still derived from the tool list rather than configured beside it, for the
    reason it always was: two switches are two ways to describe one decision,
    and the interesting bug is the pair disagreeing. A deployment that granted
    a tool and left the ceiling below it would offer the model a tool its own
    envelope denies, costing a turn that ends in `outside_submitted_envelope`.

    It used to be a chain naming `project_run` and `sandbox_run`, on the
    grounds that a name-to-risk table would be a second place a tool's risk is
    written down -- while the first place, the tool's own `ToolSpec`, is what
    the policy engine actually reads. That was right about the problem and
    settled for second best: the chain *was* the table, with two rows. Reading
    the specs is the version with no rows at all, and it is what lets ADR-0079
    produce a `read` ceiling without anybody adding a third branch for it.

    An offered name with no registered spec raises rather than defaulting. The
    two available defaults are both wrong in a way nothing downstream would
    report: `read` builds an envelope that denies a tool the turn was given,
    and `destructive` quietly raises the ceiling of every turn that contains a
    typo.
    """

    ceiling: ToolRisk = "read"
    for name in tool_names:
        risk = risks.get(name)
        if risk is None:
            raise ValueError(
                f"{name!r} is offered to a coding turn but this process has no "
                "spec for it, so its risk cannot be read"
            )
        if not risk_within(risk, ceiling):
            ceiling = risk
    return ceiling


class CodeTurnBusyError(RuntimeError):
    """This session is already running a turn."""


class CodeCapacityError(RuntimeError):
    """This process is already running as many turns as it admits."""


class CodeRunUnavailableError(RuntimeError):
    """Somebody asked to run a file and this deployment cannot run code.

    Not a 404 and not a 403: the file is there, the caller may see it, and
    nothing about the request is wrong -- ``code.sandbox_enabled`` is off, or
    the sandbox this process was told to use did not answer at boot. A 503 with
    that sentence in it is the only answer that names the fix.
    """


class CodeRunRefusedError(RuntimeError):
    """The sandbox or the working set refused one run that was asked for.

    Distinct from a script that ran and failed, which is not an error here at
    all: that is an exit code and a traceback, and it is the thing the reader
    clicked to see. This is the container that could not start, the result that
    did not parse, the output that would not fit -- states nothing about the
    request can fix, carrying the refusing side's own words.
    """


class CodeRunNotPermittedError(RuntimeError):
    """The caller does not hold the scope that running code needs.

    The same ``sandbox:run`` the tool declares, checked here because this path
    has no Policy Gateway in front of it -- there is no envelope, no step and
    no tool call, so the one gate the agent's route through this capability
    passes is a gate this route has to be. A console that asks for the scope
    and a deployment that grants it are two decisions, and this is where they
    have to meet.
    """


def new_code_session_id() -> str:
    return new_id("ses")


#: What a turn is allowed to be. Two values, not a flag, because the name is
#: what appears in the API body, in the envelope's story and in the console's
#: composer -- and `plan=false` reads as an absence where `"act"` reads as a
#: choice somebody made.
CodeMode = Literal["act", "plan"]

#: Who decides that a write happens: the model, or the person watching
#: (ADR-087).
#:
#: A second axis on the same ladder as `CodeMode`, deliberately not folded into
#: it. Flattened into one four-position control the two read as one ordering --
#: plan < ask-me < don't-ask -- and Claude Code's mode cycle does exactly that
#: flattening. It is the wrong shape *here* because the two are answered by
#: different halves of the envelope: `plan` narrows `allowed_tools`, and this
#: narrows `approval_required_risks`. A single `Literal` would have to be taken
#: apart again at the one place it is used, and the two halves would then be
#: derived from one value that names neither of them.
#:
#: There is no third position, and the missing one is the interesting one.
#: "Ask me about nothing" would have to drop `destructive`, and `destructive`
#: is `project_run` -- a command on the user's own machine, which ADR-077 says
#: is shown before it is run. So this axis may only **add** to what the
#: deployment already gates, never subtract, exactly as plan mode may only
#: narrow the tool list. A person who wants fewer questions than the ceiling
#: allows is asking the deployment, not the session.
CodeApprovals = Literal["standard", "before_write"]


@dataclass(frozen=True, slots=True)
class CodeRequest:
    """One thing a user asked a coding session to do."""

    session_id: Identifier
    instruction: str
    principal: PrincipalContext
    run_id: Identifier
    #: Whether this turn may change anything (ADR-0079). ``"plan"`` narrows the
    #: offered tools to the ones whose own `ToolSpec` says they only read, and
    #: says so in the prompt. Defaults to `"act"`, which is what every caller
    #: written before plan mode existed meant.
    mode: CodeMode = "act"
    #: Whether every write of this turn stops at a person (ADR-087).
    #: ``"standard"`` is what every caller written before this existed meant --
    #: the deployment's own gate and nothing added.
    approvals: CodeApprovals = "standard"


@dataclass(frozen=True, slots=True)
class CodeTurn:
    """What one turn produced, as the caller needs it."""

    run_id: Identifier
    report: str
    #: Where the working set stands now. ``None`` means nothing was ever
    #: written -- including by earlier turns, so it is not "this turn wrote
    #: nothing".
    workspace_version: Identifier | None
    outcome: AgentOutcome


@dataclass(slots=True)
class CodeSessionService:
    """Opens coding sessions and runs one turn of one at a time."""

    conversations: ConversationStore
    artifacts: ArtifactStore
    #: A runtime per turn, not one per process, and the reason is the approval
    #: gate: a held call is answered by a request naming a session, so the gate
    #: has to be bound to one -- and the gateway that holds the gate is built
    #: with it. Everything else about the runtime is identical between turns;
    #: what a turn costs to build is five schema validations.
    executor_for: Callable[[ApprovalScope], AgentExecutor]
    scope: WorkspaceScope
    #: No default. A turn's ceiling is a deployment decision, and a silent one
    #: is how a runaway loop becomes somebody's bill.
    budget: RunBudget
    turn_timeout_seconds: int
    max_concurrent_turns: int
    clock: Callable[[], datetime]
    tool_names: tuple[ToolName, ...] = CODE_TOOLS
    #: The other half of ADR-073's exclusivity. Read only when the session's
    #: project has a directory; a deployment that leaves either of these `None`
    #: never offers `project_*` at all, which is the correct behaviour for a
    #: build without the capability rather than a degraded one.
    project_scope: ProjectFileScope | None = None
    #: Where the project read tools leave what they handed the model, so the
    #: write tools can refuse to replace a file this turn has not seen
    #: (ADR-0078). Travels with `project_scope`: both are `None` on a build
    #: without the project capability, and the assertion beside the scope entry
    #: is the one that says so, because a deployment that wired one and not the
    #: other would be offering a write gate that never checks anything.
    read_receipts: ReadReceipts | None = None
    #: Where a tool's risk is written down: its own `ToolSpec`. Read for two
    #: decisions that used to be made from names -- the envelope's ceiling, and
    #: which tools survive into a plan turn (ADR-0079).
    #:
    #: Typed optional because it follows fields that have defaults, and refused
    #: in `__post_init__` because there is no safe value for its absence. The
    #: first draft fell back to the ceiling the name-based chain used to
    #: return, `write`; `test_a_turn_holding_the_run_tool_is_not_told_there_is_
    #: no_shell` caught it immediately -- a turn offered `project_run` under a
    #: `write` ceiling is a turn whose envelope denies the tool it was given,
    #: which ends in `outside_submitted_envelope`. Every assembly has a
    #: registry; the executor is built from one.
    tools: ToolRegistry | None = None
    projects: ProjectService | None = None
    project_tool_names: tuple[ToolName, ...] = CODE_PROJECT_TOOLS
    #: Whether each ``sandbox_run`` call stops for a human (ADR-058). Defaults
    #: to the settings default rather than contradicting it; the assembly in
    #: `apps/api/dependencies.py` always passes the configured value.
    external_requires_approval: bool = False
    #: Whether this session may search the live web (ADR-0085).
    #:
    #: A flag rather than a fifth tuple, and that is the decision rather than a
    #: shortcut. `sandbox_run` belongs only to the flat side and `project_run`
    #: only to the project side because of what those tools *are* -- one reads
    #: a ContextVar the other side never sets, the other needs a directory.
    #: `web_search` enters no scope and is equally true of both, so it is the
    #: first axis here that is genuinely orthogonal. Written as tuples it would
    #: take the four literals to eight, two of them spelled `_AND_` -- the
    #: shape ADR-077 had just finished deleting (see `CODE_PROJECT_TOOLS_WITH_
    #: RUN`). And the argument for spelling them out at all, below, is that
    #: there are fewer names than combinations; a Cartesian product retires
    #: that argument by itself.
    web_search_enabled: bool = False
    _running: set[str] = field(default_factory=set[str], init=False)
    _turns: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Refuse an assembly that wired the project side half-way (ADR-0078).

        Checked here rather than only at the scope entry, because this is where
        the mistake is made and turn time is where it would be found. A
        deployment that passed a `project_scope` and no ledger would boot,
        serve, and offer `project_write` on the user's real files with a gate
        that never checks anything -- and the transcript of the turn that
        overwrote their work would look exactly like the transcript of one that
        did not.

        One-directional on purpose. A ledger without a scope is harmless: no
        `project_*` tool is offered, so nothing records and nothing asks.
        """

        if self.project_scope is not None and self.read_receipts is None:
            raise ValueError(
                "a project-capable coding session needs read receipts: "
                "project_scope was given without read_receipts, which would "
                "offer project_write with no read-before-overwrite check"
            )
        if self.tools is None:
            raise ValueError(
                "a coding session needs the tool registry: without it the "
                "envelope's risk ceiling and plan mode's narrowing both have "
                "to be guessed from tool names"
            )

    async def open(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        title: str | None = None,
    ) -> Identifier:
        session_id = new_code_session_id()
        await self.conversations.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=principal_id,
            title=title,
            mode="code",
        )
        return session_id

    async def sessions(
        self, *, tenant_id: str, principal_id: str, limit: int = 50
    ) -> tuple[ConversationSession, ...]:
        """This principal's coding sessions, most recently spoken in first.

        Server-side rather than in the browser, which is where this list used
        to live. A list kept only in `localStorage` answers "what did I do on
        this machine, in this browser, since I last cleared it" -- and the
        sessions it forgets are still there, still owned, and no longer
        reachable, because a session id is the only way in.
        """

        return await self.conversations.list_sessions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
            limit=limit,
        )

    async def rename(
        self, *, session_id: str, tenant_id: str, principal_id: str, title: str
    ) -> ConversationSession:
        """Replace the name a session was given by its first instruction."""

        return await self.conversations.rename_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            title=title,
            mode="code",
        )

    async def delete(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> None:
        """Remove one coding session, its transcript and its event stream.

        ``mode="code"`` is fixed here for the reason every other method on this
        service fixes it: a caller that could hand this one a chat session id
        would be deleting a conversation this service never ran.

        The workspace artifacts stay, unreachable rather than removed (ADR-056
        §5) -- the same thing that already happens to a workspace version each
        time a write supersedes it.
        """

        await self.conversations.delete_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )

    async def history(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> tuple[StoredMessage, ...]:
        """This principal's own coding conversation, oldest first.

        The mode is fixed here for the same reason ``ChatService.history``
        fixes its own: this service is the Code one, and a caller able to ask
        it for a chat session's history would be reading a conversation whose
        turns it never ran.
        """

        stored = await self.conversations.history(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        # The whole `StoredMessage`, not the `Message` inside it. What the turn
        # spent hangs off the outer record; unwrapping one layer here dropped
        # it, and the caller's reason for asking is that number.
        return stored

    async def workspace(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> tuple[WorkspaceListing, ...]:
        """What this session's working set currently holds.

        The whole product of a coding session is these files, and until this
        existed the only way to see one was to ask the agent to read it back --
        which spends a turn and a model call to answer a question the store
        already knows.

        Reachable by name only. The version is read from the session row, never
        taken from the caller: a principal who could name a version could name
        one belonging to another of their own sessions, and the artifact store
        scopes reads to a tenant and a principal and nothing narrower. That is
        an architecture test, not a habit -- see
        ``tests/architecture/test_a_workspace_version_is_never_asked_for.py``.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        return await Workspace(
            artifacts=self.artifacts,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ).list(session.workspace_version)

    async def put_workspace_file(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        name: str,
        content: bytes,
        media_type: str,
    ) -> tuple[WorkspaceListing, ...]:
        """Put a file a *person* supplied into this session's working set.

        The counterpart to `open_workspace_file`, and the half a coding session
        was missing: an agent could produce files and read them back, and there
        was no way to hand it one. Until this existed, giving a session a log to
        look at meant pasting it into the instruction -- which spends context on
        content the workspace is built to hold, and truncates anything large.

        Binary types are allowed here, and deliberately so. `WorkspaceWriteTool`
        refuses docx, xlsx, pptx and pdf, and that refusal is about what the
        *model* may synthesise: a model emitting what it claims are docx bytes
        is producing something no reader can trust. A person attaching a PDF is
        the opposite situation -- the bytes are the thing they have, and the
        session's job is to look at them.

        Reuses `SessionWorkspace`, so the version pointer advances with the same
        compare-and-set every tool write uses. An upload racing a running turn
        loses that comparison and is refused, rather than leaving the session
        pointing at a manifest that names only the uploaded file.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        workspace = SessionWorkspace(
            workspace=Workspace(
                artifacts=self.artifacts,
                tenant_id=tenant_id,
                principal_id=principal_id,
            ),
            conversations=self.conversations,
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        version = await workspace.write(
            session.workspace_version, name, content, media_type=media_type
        )
        return await workspace.list(version)

    async def workspace_session(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> WorkspaceSession:
        """This session's working set, opened for writing, for one caller.

        The same object a turn runs inside, handed out so that something other
        than a turn can advance the working set -- today that is the console's
        运行 button (ADR-065), which runs one file the reader is looking at.

        Authorization is the first call and there is no second one: the store
        refuses another tenant, another principal and a chat session id
        identically, and where the files are is only learned after it has not
        refused. What comes back is a *writer* -- ``SessionWorkspace`` records
        each version on the session row as it commits -- so a caller that runs
        code with it leaves the same trail a tool call would, and a caller that
        crashes half way leaves the files it had finished. That is the same
        per-write pointer every other Code write moves; see
        ``application/session_workspace.py`` for why it is not deferred to the
        end.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        return self._workspace_at(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            version=session.workspace_version,
        )

    def _workspace_at(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        version: Identifier | None,
    ) -> WorkspaceSession:
        """The writer, from a version somebody has already been authorized for.

        Deliberately not `async` and deliberately taking the version rather
        than reading it: the read is the authorization, and a constructor that
        performed its own would be a second gate behind the first -- whichever
        fires first hides the other, and the covered one is then whichever
        happens to be written first (the same argument ``_run`` makes about the
        history read it does not repeat the mode on).
        """

        return WorkspaceSession(
            workspace=SessionWorkspace(
                workspace=Workspace(
                    artifacts=self.artifacts,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                ),
                conversations=self.conversations,
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            ),
            version=version,
        )

    async def open_workspace_file(
        self, *, session_id: str, tenant_id: str, principal_id: str, name: str
    ) -> tuple[ArtifactRef, AsyncIterator[bytes]]:
        """One of this session's files: what it is, and a stream of its bytes.

        Both together, from one store, on purpose. The caller needs the
        reference for its headers and the stream for its body, and the first
        shape this had returned only the reference -- leaving the route to
        stream from `dependencies.artifacts`, a *second* handle to what must be
        the same store. Nothing guaranteed it was: the API's test harness
        builds exactly that world by accident, which is evidence enough that
        production could. Two handles means headers describing a file from one
        store and bytes arriving from another.

        The artifact id never leaves this process. It is what the listing
        deliberately withholds; a client holding one could address a version
        this session has already moved past.

        `iter_chunks` is called here rather than awaited later because it is
        deliberately not `async def` (see the `ArtifactStore` port): its
        authorization runs at call time, so a refusal happens while the caller
        can still choose a status code.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        entry = await Workspace(
            artifacts=self.artifacts,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ).locate(session.workspace_version, name)
        chunks = self.artifacts.iter_chunks(
            tenant_id=tenant_id,
            artifact_id=entry.artifact_id,
            principal_id=principal_id,
        )
        return entry, chunks

    async def ask(
        self,
        request: CodeRequest,
        sink: ProcessOnlySink,
        cancellation: CancellationToken,
    ) -> CodeTurn:
        """Run one turn: append what was asked, work, append the report."""

        # Admission first, and both checks are fail-fast with no await between
        # test and claim -- which is what makes them atomic here.
        if self._turns >= self.max_concurrent_turns:
            raise CodeCapacityError(
                f"this process runs at most {self.max_concurrent_turns} "
                "coding turns at once"
            )
        if request.session_id in self._running:
            raise CodeTurnBusyError("this session is already running a turn")
        self._turns += 1
        self._running.add(request.session_id)
        try:
            return await self._run(request, sink, cancellation)
        finally:
            self._running.discard(request.session_id)
            self._turns -= 1

    async def _run(
        self,
        request: CodeRequest,
        sink: ProcessOnlySink,
        cancellation: CancellationToken,
    ) -> CodeTurn:
        principal = request.principal
        # Authorization and the workspace pointer arrive together: a caller who
        # may not address this session must not learn where its files are.
        session = await self.conversations.session(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            mode="code",
        )

        # Read without repeating the mode, deliberately. The call above is this
        # turn's one authorization, and a second gate behind it would be a gate
        # nothing can test: whichever fires first hides the other, and the
        # covered one is then whichever happens to be written first.
        stored = await self.conversations.history(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )
        history = tuple(record.message for record in stored)
        asked = user_message(request.instruction)
        # Appended before the run, not after it. What the user said is a fact
        # the moment they said it, and a turn that fails must not lose the
        # sentence that caused it.
        await self.conversations.append(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            messages=(asked,),
        )

        # Named here rather than at creation, because a coding session is opened
        # before any instruction exists -- the console's "new session" button
        # has nothing to name it with. A client could rename it after the first
        # turn returned, but a closed tab or a dropped connection would lose
        # exactly the session that then cannot be found again, which is the
        # failure this exists to remove.
        #
        # The read above only decides whether to bother; the store's
        # `WHERE title IS NULL` is the arbiter, so a retry writes nothing and a
        # name somebody typed is never overwritten.
        if session.title is None:
            derived = title_from_instruction(request.instruction)
            if derived is not None:
                await self.conversations.set_title_if_unset(
                    session_id=request.session_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    title=derived,
                    mode="code",
                )

        workspace = self._workspace_at(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            version=session.workspace_version,
        )

        # One workspace for the whole turn, entered around the run rather than
        # around each tool: the tools find it through a ContextVar, and a scope
        # entered per call would hand each of them a version the last one had
        # already moved past.
        executor = self.executor_for(
            ApprovalScope(
                tenant_id=principal.tenant_id,
                session_id=request.session_id,
                principal_id=principal.principal_id,
            )
        )
        # Which file language this turn speaks, decided here and frozen for the
        # turn (ADR-073 §5.2). Deciding it per tool call would let somebody
        # registering a directory mid-turn change what the running model is
        # holding, and the envelope was already signed with the other list.
        project_files = await self._project_files_for(
            principal=principal, project_id=session.project_id
        )
        tool_names = (
            self.tool_names if project_files is None else self.project_tool_names
        )
        # The one append, in the statement that already freezes the file
        # language, so a turn has one moment where "what am I holding" is
        # answered rather than two.
        #
        # Before `read_only` below, and the consequence is that a **plan turn
        # loses it**: `read_only` keeps `risk == "read"` and `web_search` is
        # `external`. That is the right outcome from the wrong-looking rule, so
        # it is worth saying which. Plan mode narrows by risk on purpose
        # (ADR-0079) -- a name-suffix filter would be a second place a tool's
        # risk is written down -- and the risk is `external` for the reason
        # `domain/research.py` gives: the question leaves this process. A plan
        # turn that could put the user's question on the open web would be
        # doing something a turn "that cannot change anything" should not.
        #
        # It is still a gap for the reader who wanted to research before
        # planning, and it is recorded as one (F-27) rather than papered over
        # with an exception here.
        if self.web_search_enabled:
            tool_names = (*tool_names, WEB_SEARCH_TOOL)
        # Frozen here, beside the file-language decision ADR-073 §5.2 freezes
        # in the same statement and for the same reason: deciding it per tool
        # call would let something change what the running model is holding
        # after the envelope had been signed with the other list.
        risks = self._risks()
        if request.mode == "plan":
            tool_names = read_only(tool_names, risks=risks)
        with ExitStack() as scopes:
            if project_files is None:
                scopes.enter_context(self.scope.using(workspace))
            else:
                # Only one is entered. Entering both would leave the other set's
                # tools live in this turn's context -- and the tools find their
                # backing through the ContextVar, not through the envelope, so
                # an envelope that forgot to exclude one would become reachable.
                assert self.project_scope is not None
                scopes.enter_context(self.project_scope.using(project_files))
                # A fresh, empty ledger per turn (ADR-0078). Entered here
                # rather than around the session, because a receipt is a claim
                # that the model has *just* seen a file: carried into the next
                # turn it would be a claim about a file as it stood minutes ago
                # and would licence exactly the overwrite this gate exists to
                # refuse. Project side only -- the flat workspace versions
                # every write, so "did you read it first" is a question about
                # something that can be recovered either way.
                assert self.read_receipts is not None
                scopes.enter_context(self.read_receipts.using())
            outcome = await executor.run(
                self._request_for(
                    request,
                    history=history,
                    asked=asked,
                    tool_names=tool_names,
                    risks=risks,
                ),
                sink,
                cancellation,
            )

        if outcome.output_text:
            # Only when there is one. A failed or cancelled run produces no
            # report, and an empty assistant message would read as one.
            await self.conversations.append(
                session_id=request.session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                messages=(assistant_message(text=outcome.output_text),),
            )

        return CodeTurn(
            run_id=request.run_id,
            report=outcome.output_text,
            # Read from the object the tools actually advanced, not from the
            # row: the pointer was written through per write, so this is the
            # same value and one fewer round trip.
            workspace_version=workspace.version,
            outcome=outcome,
        )

    def _risks(self) -> Mapping[ToolName, ToolRisk]:
        """Every registered tool's risk, from the specs themselves.

        Read per turn rather than cached, and that costs nothing worth saving:
        the registry is frozen at process start, so this is a dict comprehension
        over a tuple that never changes. Caching it would add a second place the
        answer lives, for a saving smaller than one schema validation.
        """

        assert self.tools is not None  # refused in `__post_init__`
        return {spec.name: spec.risk for spec in self.tools.specs()}

    async def _project_files_for(
        self, *, principal: PrincipalContext, project_id: str | None
    ) -> ProjectFileStore | None:
        """This session's project directory, or ``None`` for the flat workspace.

        ``None`` on every path that is not unambiguously "this session belongs
        to a project that has a registered directory": no project, no directory,
        or a deployment without the capability. The fallback is the flat
        workspace, which always works -- so a misconfiguration costs the
        directory, never the turn.
        """

        if project_id is None or self.projects is None or self.project_scope is None:
            return None
        try:
            return await self.projects.open_files(principal, project_id)
        except NotFoundError:
            # The project has no directory registered, or is not this
            # principal's. Both mean "use the workspace", and neither is worth
            # failing a turn over.
            return None

    def _request_for(
        self,
        request: CodeRequest,
        *,
        history: tuple[Message, ...],
        asked: Message,
        tool_names: tuple[ToolName, ...],
        risks: Mapping[ToolName, ToolRisk],
    ) -> AgentRunRequest:
        return AgentRunRequest(
            trace=TraceContext(agent_run_id=request.run_id),
            run_kind="code",
            # One stream per session, so a subscriber follows the conversation
            # rather than one turn of it.
            stream_id=request.session_id,
            principal=request.principal,
            envelope=AuthorizationEnvelope(
                allowed_tools=tool_names,
                # Derived from what the offered tools say about themselves,
                # rather than configured beside them: two switches are two ways
                # to describe one decision, and the interesting bug is the pair
                # disagreeing -- a deployment that granted a tool and left the
                # ceiling below it offers the model a tool its own envelope
                # denies, costing a turn that ends in
                # `outside_submitted_envelope`. It admits `read` for a plan
                # turn, `write` for an ordinary one, `external` where the
                # sandbox is granted and `destructive` where `project_run` is,
                # and none of those four is a branch anybody wrote here.
                max_tool_risk=code_risk_ceiling(tool_names, risks=risks),
                # `destructive` is armed unconditionally. It was armed before
                # anything could trigger it, and ADR-077 then granted something
                # that does: `project_run` runs a command on the user's own
                # machine, and every call stops at a human. Whether `external`
                # joins it is ADR-058's
                # question: the gate F-05 armed early turned out to buy latency
                # rather than consent (the card shows a digest, ADR-054), and
                # the Task path has always run the same `sandbox_run` ungated,
                # so the deployment now says which arrangement it wants.
                # Both paragraphs still hold; what is new is that this line
                # now also reads a fact about the turn itself --
                # `request.approvals` (ADR-087). It can only *add* `write` to
                # this set and can subtract nothing, and the shape of
                # `code_approval_risks` is that invariant rather than a
                # sentence about it.
                approval_required_risks=code_approval_risks(
                    request.approvals,
                    external_requires_approval=self.external_requires_approval,
                ),
            ),
            budget=self.budget.model_copy(
                update={
                    "deadline": self.clock()
                    + timedelta(seconds=self.turn_timeout_seconds)
                }
            ),
            # Selected from the same facts the envelope reads, so the model is
            # never told it cannot do something it has been granted -- nor that
            # it will wait for a human who is not going to be asked (ADR-058:
            # the gated text says "expect to wait, do not spend one", which is
            # an instruction to avoid the tool this deployment just freed).
            system_prompt=_system_prompt_for(
                tool_names,
                external_requires_approval=self.external_requires_approval,
                plan_only=request.mode == "plan",
                write_gate=request.approvals == "before_write",
            ),
            messages=(*history, asked),
            # Both, and they are not the same thing: the envelope says what
            # policy would permit, `tool_names` is what the model is offered.
            tool_names=tool_names,
        )

    async def drain_cleanup(self, *, timeout_seconds: float) -> None:
        """Wait for turns in flight, so a deploy does not cut one in half.

        Nothing is cancelled here and nothing is recovered afterwards -- there
        is no half-finished state to reclaim, so the only thing worth doing is
        giving a turn that is nearly done the chance to finish. A turn that
        needs longer than the grace period is cut off, and its workspace stands
        at its last successful write. ``docs/known-gaps.md`` F-02 records the
        arithmetic.
        """

        deadline = self.clock() + timedelta(seconds=timeout_seconds)
        while self._turns and self.clock() < deadline:
            await asyncio.sleep(0.05)


# Called here rather than beside its definition: it evaluates
# `_system_prompt_for`, which is defined further down this module. Import
# order is the only reason for the distance.
_assert_every_prompt_combination_resolves()

__all__ = [
    "CODE_PROJECT_TOOLS",
    "CODE_PROJECT_TOOLS_WITH_RUN",
    "CODE_TOOLS",
    "CODE_TOOLS_WITH_SANDBOX",
    "CodeApprovals",
    "CodeCapacityError",
    "CodeMode",
    "CodeRequest",
    "CodeRunNotPermittedError",
    "CodeRunRefusedError",
    "CodeRunUnavailableError",
    "CodeSessionService",
    "CodeTurn",
    "CodeTurnBusyError",
    "code_approval_risks",
    "code_risk_ceiling",
    "new_code_session_id",
    "read_only",
]
