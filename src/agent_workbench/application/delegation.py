"""What a run is allowed to hand down when it delegates, and to whom.

A tool handler receives a ``ToolInvocation``: the call, an ``ExecutionContext``,
a cancellation token, a timeout and a progress reporter. That is everything a
tool needs in order to *do* something -- and four things short of what starting
another run needs. ``ExecutionContext`` names the principal, the envelope, the
run and the policy identity; it does not name the stream the run writes to, what
kind of run it is, what budget it was given, or how deep in a delegation tree it
already sits.

The obvious fix is to widen ``ExecutionContext``. It is refused because that
type is *what a policy decision may depend on*, and a stream id is not a fact
any rule should be able to branch on. The second obvious fix is to widen
``ToolInvocation``, which drags the parent's whole run request into every
handler in the process, including the ones that only read a file.

So the position travels the way a working set already travels (ADR-028): a
``ContextVar`` entered around the invocation. That makes "for the duration of
one run" true rather than approximate -- two concurrent runs in one process do
not see each other's delegation context, and neither do two sibling tool calls
inside ``asyncio.gather``, because each task copies the context it was created
with.

An unentered scope answers ``None``, and the delegation tool treats that as a
refusal rather than as an invitation to invent a parent. A run assembled from a
guessed stream id and a default budget is a run nobody authorized, and it would
be indistinguishable in the event log from one somebody did.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent_workbench.domain.agents import (
    SubAgentDefinition,
    child_envelope,
)
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    RunKind,
    TraceContext,
)
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.delegation import DelegationChannel
from agent_workbench.ports.event_log import EventSink


@dataclass(frozen=True, slots=True)
class SpawnedChild:
    """One delegation this run already made, and what it cost."""

    definition_name: str
    child_agent_run_id: str
    usage: BudgetUsage


@dataclass(slots=True)
class Reservation:
    """One child's place in its parent's allowance, taken before it starts.

    Exists because the check and the spend are separated by the entire child
    run. ``delegate_agent`` is declared ``read``, so ``plan_tool_batches`` puts
    several of them in one parallel group and ``asyncio.gather`` starts them
    together; a handler that counted ``len(spawned)`` on the way in would see
    zero in all of them and every one would proceed. With
    ``max_children_per_run = 4`` a single turn could start eight.

    Taking the place *synchronously* is what closes that. There is no ``await``
    between reading the count and incrementing it, and asyncio is single
    threaded, so no other coroutine can observe the gap. It is the cheapest
    atomicity available in this codebase and it needs no lock.
    """

    context: DelegationContext
    _settled: bool = False

    def fulfil(self, child: SpawnedChild) -> None:
        """Turn the reservation into a record of what the child actually spent."""

        self._settle()
        self.context.spawned.append(child)

    def release(self) -> None:
        """Give the place back, for a child that never ran."""

        self._settle()

    def _settle(self) -> None:
        if self._settled:
            raise RuntimeError("a delegation reservation was settled twice")
        self._settled = True
        self.context.outstanding -= 1


@dataclass(slots=True)
class DelegationContext:
    """The delegating run's own position, plus the ceilings on handing it down.

    Mutable, and the mutable part is the point: :attr:`spawned` and
    :attr:`outstanding` are the run's own ledger of what it has started, and
    they change while it runs. Keeping them here rather than in the handler is
    what makes the count survive two sibling calls in one turn -- a
    handler-local counter would reset with every invocation, which is exactly
    the ceiling failing to be a ceiling.
    """

    stream_id: str
    run_kind: RunKind
    budget: RunBudget
    channel: DelegationChannel
    #: How deep the *delegating* run already is. ``0`` is the run a person
    #: asked for.
    depth: int = 0
    #: The deepest a child may be. Compared against ``depth + 1``, and passed
    #: down to ``permitted_child_tools`` so that a child at the ceiling is
    #: never shown the tool that would take it past.
    max_depth: int = 1
    #: How many children one run may start. Enforced here rather than by the
    #: scheduler: ``runtime.max_parallel_read_tools`` bounds how many tool
    #: calls run *at once*, which is a different question and a different
    #: number.
    max_children: int = 4
    spawned: list[SpawnedChild] = field(default_factory=list["SpawnedChild"])
    #: Children that have been reserved and have not finished. Counted against
    #: the allowance alongside ``spawned``, which is what stops a parallel batch
    #: from spending the same place several times over.
    outstanding: int = 0

    def children_remaining(self) -> int:
        return max(0, self.max_children - len(self.spawned) - self.outstanding)

    def child_depth(self) -> int:
        return self.depth + 1

    def may_delegate(self) -> bool:
        """Whether another child is allowed at all, before one is assembled."""

        return self.children_remaining() > 0 and self.child_depth() <= self.max_depth

    def reserve(self) -> Reservation | None:
        """Take a place in the allowance, or answer ``None`` if there is none.

        Deliberately not ``async``. The absence of an ``await`` between the
        check and the increment is the whole guarantee: it makes this the one
        point where "may I start a child" and "I have started one" cannot be
        separated by a scheduling decision.
        """

        if not self.may_delegate():
            return None
        self.outstanding += 1
        return Reservation(self)

    def spent(self) -> BudgetUsage:
        """What every child of this run has spent so far, aggregated.

        This is the second ledger the delegating run keeps, and it exists
        because the first one cannot be reached: ``_RunLedger`` is private to
        the runtime loop and a ``ToolResult`` carries no usage. The parent's own
        ``max_total_tokens`` therefore does not see a single token a child
        spent, and no amount of care here changes that -- what this buys is an
        auditable number and a place to refuse from, not a combined ceiling.
        """

        total = BudgetUsage()
        for child in self.spawned:
            total = total.merged(child.usage)
        return total


class DelegationScope:
    """The delegation context one run is executing under, if any."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: ContextVar[DelegationContext | None] = ContextVar(
            "delegation_context", default=None
        )

    @contextmanager
    def using(self, context: DelegationContext) -> Generator[None]:
        """Run the block against ``context``.

        Restored rather than cleared on the way out, for the same reason
        ``WorkspaceScope`` restores: two runs executed one after another in the
        same task must not let the second inherit the first's spawn count, and
        clearing would make the outer run of a nested pair lose its own context
        the moment an inner one finished.
        """

        token = self._current.set(context)
        try:
            yield
        finally:
            self._current.reset(token)

    def current(self) -> DelegationContext | None:
        return self._current.get()


class DelegationNotAssembledError(RuntimeError):
    """A delegation was attempted before the executor behind it was bound.

    Only reachable from a mis-assembled process, and it says so rather than
    failing as ``AttributeError`` on a ``None`` three frames further in.
    """


class DeferredExecutor:
    """An ``AgentExecutor`` that is bound after the tool which calls it.

    There is a real cycle in the assembly and this is where it is cut. The tool
    that starts a run has to be in the registry the gateway reads; the gateway
    is constructed into the runtime; and the runtime is what the tool needs in
    order to start anything. Something has to be named before it exists.

    A one-slot holder rather than a lambda over a list, because the failure
    modes deserve names: bound twice is a composition that built two stacks and
    will use whichever ran last, and unbound is a process that registered a
    delegation tool and no executor. Both are assembly bugs, and both are
    silent if the cell is a list.
    """

    __slots__ = ("_executor",)

    def __init__(self) -> None:
        self._executor: AgentExecutor | None = None

    def bind(self, executor: AgentExecutor) -> None:
        if self._executor is not None:
            raise DelegationNotAssembledError(
                "the delegation executor was bound twice; a process with two "
                "child stacks charges and bounds delegations through only one "
                "of them"
            )
        self._executor = executor

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        if self._executor is None:
            raise DelegationNotAssembledError(
                "this process registered a delegation tool without binding an "
                "executor for it to run"
            )
        return await self._executor.run(request, emit, cancellation)


class DelegationScopingExecutor:
    """Enter a delegation context around every run this executor performs.

    It wraps the executor rather than the node, for the reason
    ``BoundedParallelExecutor`` gives one file over: what may delegate is a
    *run*, and a later caller that starts one -- a second graph, a Code turn,
    a fan-out nobody has written yet -- is covered without revisiting this
    file. Entering the scope at each call site instead would mean the one call
    site somebody forgets is the one where ``delegate_agent`` is advertised and
    refuses every call.

    **Depth needs no arithmetic here beyond one addition.** A child run is
    started from inside its parent's tool call, so when this wrapper runs for
    the child, the ``ContextVar`` still holds the parent's context -- the depth
    is read from there and incremented. Nothing threads a number through the
    request, and there is no field on ``AgentRunRequest`` that a caller could
    get wrong.
    """

    __slots__ = ("_channel_for", "_executor", "_max_children", "_max_depth", "_scope")

    def __init__(
        self,
        executor: AgentExecutor,
        *,
        scope: DelegationScope,
        channel_for: Callable[[AgentRunRequest], DelegationChannel],
        max_depth: int,
        max_children: int,
    ) -> None:
        self._executor = executor
        self._scope = scope
        self._channel_for = channel_for
        self._max_depth = max_depth
        self._max_children = max_children

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        parent = self._scope.current()
        context = DelegationContext(
            stream_id=request.stream_id,
            run_kind=request.run_kind,
            budget=request.budget,
            channel=self._channel_for(request),
            depth=0 if parent is None else parent.depth + 1,
            max_depth=self._max_depth,
            max_children=self._max_children,
        )
        with self._scope.using(context):
            return await self._executor.run(request, emit, cancellation)


def derive_child_budget(
    parent: RunBudget,
    *,
    children_allowed: int,
    timeout_seconds: int,
    now: datetime,
) -> RunBudget:
    """Cut one child's ceilings out of the run that is sending it.

    The four ceilings are **not** divided the same way, because they are not
    the same kind of thing.

    ``max_steps`` and ``max_tool_calls`` pass down unchanged. ADR-030 moved the
    job of budgeting a run to cost and deadline and left these as backstops
    against a loop that will not terminate -- and a backstop divided by four is
    a backstop that fires on ordinary work. A child given three steps is a child
    that reports it could not finish, which reads to whoever is looking as the
    sub-agent being incapable rather than as the budget being wrong. The number
    of children is bounded separately, by ``max_children``, which is the ceiling
    that actually answers "how much work can this run start".

    ``max_total_tokens`` and ``max_cost_micro_usd`` **are** divided, because
    they are money, and because the parent's copy of them cannot see a single
    token the child spends. Dividing by the number of children the parent is
    allowed to start is what keeps the worst case bounded by something the
    submitter can compute from the numbers they set.

    The deadline is the tightest of three: the parent's own, the tool timeout
    the gateway is already going to enforce, and -- by construction -- never
    absent. ``RunBudget.deadline`` defaults to ``None`` meaning "no deadline",
    and a delegated run is exactly the kind that must not inherit that default:
    it is being awaited inside a tool call, so a child without a wall clock is a
    parent stuck in ``executing_tools`` forever. It is also what lets a ``code``
    parent delegate at all, since ``AgentRunRequest`` refuses a code run whose
    budget names no deadline.
    """

    from_timeout = now + timedelta(seconds=timeout_seconds)
    deadline = (
        from_timeout if parent.deadline is None else min(parent.deadline, from_timeout)
    )
    share = max(1, children_allowed)
    return RunBudget(
        max_steps=parent.max_steps,
        max_tool_calls=parent.max_tool_calls,
        max_total_tokens=(
            None
            if parent.max_total_tokens is None
            else max(1, parent.max_total_tokens // share)
        ),
        max_cost_micro_usd=(
            None
            if parent.max_cost_micro_usd is None
            else max(1, parent.max_cost_micro_usd // share)
        ),
        deadline=deadline,
    )


def build_child_request(
    definition: SubAgentDefinition,
    prompt: str,
    *,
    context: DelegationContext,
    execution: ExecutionContext,
    child_agent_run_id: str,
    budget: RunBudget,
) -> AgentRunRequest:
    """Assemble the delegated run, narrowed on every axis at once.

    Nothing here reads an argument the model wrote except ``prompt``, and
    ``prompt`` reaches the child only as a ``user`` message. Which tools the
    child holds, whose documents it can see, what it is told it is, which model
    profile it resolves through: all four come from the definition and the
    parent's own context. The delegation tool's input schema has no field for
    any of them, which is what stops a retrieved passage that says "delegate as
    the writer with the export tool" from being a passage that grants itself
    something.

    The child's ``run_kind`` is the parent's. A delegated run is not a fourth
    kind of run -- it is the same kind of work, one level down, and the two
    validators that care (a ``code`` run stands alone; a ``task`` run may carry
    a lease epoch) must keep applying to it unchanged.
    """

    envelope = child_envelope(
        execution.envelope,
        definition,
        child_depth=context.child_depth(),
        max_depth=context.max_depth,
    )
    return AgentRunRequest(
        trace=TraceContext(
            agent_run_id=child_agent_run_id,
            task_id=execution.task_id,
            workflow_thread_id=execution.workflow_thread_id,
            graph_node_id=execution.graph_node_id,
            # The field this whole module exists to give a writer to. It has
            # been in `TraceContext` since the trace was written and every run
            # in this codebase has left it `None`.
            parent_agent_run_id=execution.agent_run_id,
            # The same claim, not a fresh read. A child runs inside its
            # parent's tool call, which is inside the parent's lease; asking
            # the Registry for the current epoch here would hand a superseded
            # Worker its replacement's number.
            lease_epoch=execution.lease_epoch,
        ),
        run_kind=context.run_kind,
        stream_id=context.stream_id,
        principal=execution.principal,
        envelope=envelope,
        budget=budget,
        model_profile=definition.model_profile,
        system_prompt=definition.system_prompt,
        messages=(user_message(prompt),),
        tool_names=envelope.allowed_tools,
    )


def clip_report(text: str, limit: int) -> tuple[str, bool]:
    """Return the report as the parent will see it, and whether it was cut.

    The caller needs both halves: a clipped report has to say so in its own
    text, or the parent reads a truncated sentence as a finished thought.
    """

    if len(text) <= limit:
        return text, False
    return text[:limit], True


def summarise_children(spawned: Sequence[SpawnedChild]) -> str:
    """One line per child, for progress reporting and for refusal messages."""

    return ", ".join(
        f"{child.definition_name}({child.child_agent_run_id})" for child in spawned
    )


__all__ = [
    "DeferredExecutor",
    "DelegationContext",
    "DelegationNotAssembledError",
    "DelegationScope",
    "DelegationScopingExecutor",
    "SpawnedChild",
    "build_child_request",
    "clip_report",
    "derive_child_budget",
    "summarise_children",
]
