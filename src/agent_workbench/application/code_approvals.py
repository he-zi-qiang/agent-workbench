"""Where a held tool call waits, and where a human's answer reaches it.

One process, one registry, one dictionary of questions that are currently on
somebody's screen. That is the whole mechanism, and it is only sound because
Code runs in the API process: the coroutine that is waiting and the request
that answers are in the same event loop, so a future is enough. A deployment
whose runs happen elsewhere gets no gate at all -- see
``ports/approval_gate.py`` for why that is a statement rather than a gap.

Two things here are not obvious and both are deliberate.

**A question is addressed by more than its id.** An approval id is a uuid4 and
guessing one is not the threat; naming one you are not entitled to is. So a
decision has to arrive with the tenant, the session and the principal it
belongs to, and a mismatch on any of them is answered exactly like an id that
does not exist. "This id is real, just not yours" is a fact about somebody
else's session.

**A standing rule is about a call, not about a tool.** The policy engine
decides that approval is required from the tool's declared risk and never reads
the arguments, so "approve `workspace_write` for this session" would let one
approved write stand for every later one. A rule is therefore keyed by the
arguments as well, through the digest the gateway already published on
``ToolProposed`` -- and for a tool whose risk is external or destructive there
is no standing rule at all, because a blanket yes to an irreversible effect is
the thing that must be asked every time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import ApprovalDecidedBy, ApprovalDecision
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.tools import PermissionScope, ProposedToolName, ToolRisk

#: Risks for which ``approve_for_session`` is refused. An external or
#: destructive effect is exactly the kind whose second occurrence deserves the
#: same question as its first.
UNREPEATABLE_RISKS: frozenset[ToolRisk] = frozenset({"external", "destructive"})


class ApprovalNotPendingError(RuntimeError):
    """This approval was already decided, or the run stopped waiting for it."""


class StandingApprovalRefusedError(RuntimeError):
    """A blanket yes was asked for where only a single yes is available."""


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    """Whose session a held call belongs to."""

    tenant_id: str
    session_id: str
    principal_id: str


@dataclass(slots=True)
class _Pending:
    scope: ApprovalScope
    tool_name: ProposedToolName
    argument_digest: str
    risk: ToolRisk | None
    answer: asyncio.Future[tuple[ApprovalDecision, ApprovalDecidedBy]]


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One question, as a reader of the session's approvals would see it."""

    approval_id: Identifier
    tool_name: ProposedToolName
    argument_digest: str
    risk: ToolRisk | None


@dataclass(slots=True)
class CodeApprovalRegistry:
    """Every question this process currently has open, and the rules it kept."""

    _pending: dict[str, _Pending] = field(
        default_factory=dict[str, _Pending], init=False
    )
    #: (tenant, session, tool, digest) -> the standing yes a human left behind.
    _rules: set[tuple[str, str, str, str]] = field(
        default_factory=set[tuple[str, str, str, str]], init=False
    )

    def gate_for(self, scope: ApprovalScope) -> SessionApprovalGate:
        """The gate one session's tool gateway asks.

        Session-scoped by construction rather than by parameter: the gateway
        has an ``ExecutionContext``, which carries a run and a principal but no
        session, so a gate that took the session as an argument would be taking
        it from something that does not know it.
        """

        return SessionApprovalGate(registry=self, scope=scope)

    def pending(self, scope: ApprovalScope) -> tuple[PendingApproval, ...]:
        return tuple(
            PendingApproval(
                approval_id=approval_id,
                tool_name=held.tool_name,
                argument_digest=held.argument_digest,
                risk=held.risk,
            )
            for approval_id, held in self._pending.items()
            if held.scope == scope
        )

    def decide(
        self,
        *,
        approval_id: str,
        scope: ApprovalScope,
        decision: ApprovalDecision,
    ) -> None:
        """Answer one held call, once.

        Removal and resolution happen together and without an await between
        them, so a second decision for the same id cannot find the question
        still open. That is the whole of the idempotency story: there is no
        window, rather than a window somebody has to remember to close.
        """

        held = self._pending.get(approval_id)
        # A question that is not this caller's is a question that does not
        # exist, and the two answers are deliberately identical.
        if held is None or held.scope != scope:
            raise NotFoundError("approval not found")
        if held.answer.done():  # pragma: no cover - resolved entries are removed
            raise ApprovalNotPendingError("approval already decided")
        if decision == "approve_for_session" and held.risk in UNREPEATABLE_RISKS:
            raise StandingApprovalRefusedError(
                f"{held.tool_name} is {held.risk}: it may be approved once, "
                "not for the session"
            )

        del self._pending[approval_id]
        if decision == "approve_for_session":
            self._rules.add(
                (
                    scope.tenant_id,
                    scope.session_id,
                    held.tool_name,
                    held.argument_digest,
                )
            )
        held.answer.set_result((decision, "human"))

    def has_standing_rule(
        self, scope: ApprovalScope, tool_name: str, argument_digest: str
    ) -> bool:
        return (
            scope.tenant_id,
            scope.session_id,
            tool_name,
            argument_digest,
        ) in self._rules

    def hold(self, approval_id: str, held: _Pending) -> None:
        self._pending[approval_id] = held

    def release(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)


@dataclass(frozen=True, slots=True)
class SessionApprovalGate:
    """One session's answer to "may this call run", for the tool gateway."""

    registry: CodeApprovalRegistry
    scope: ApprovalScope

    async def request(
        self,
        *,
        approval_id: Identifier,
        tool_call_id: Identifier,
        tool_name: ProposedToolName,
        argument_digest: str,
        risk: ToolRisk | None,
        required_scopes: tuple[PermissionScope, ...],
        timeout_seconds: float,
    ) -> tuple[ApprovalDecision, ApprovalDecidedBy]:
        """Answer from a standing rule if there is one; otherwise ask and wait.

        The rule is consulted first and answers without anybody seeing a
        question, which is what a standing yes is for. It matches on the
        arguments, so the same tool with different arguments is a new question.

        Waiting parks on a future the deciding request resolves. The gateway
        races this against the run's cancellation and cancels the loser, which
        is why the withdrawal below is in a ``finally``: a cancelled run must
        not leave a question on a screen that nothing is listening to.
        """

        if self.registry.has_standing_rule(self.scope, tool_name, argument_digest):
            return "approve_for_session", "session_rule"

        answer: asyncio.Future[tuple[ApprovalDecision, ApprovalDecidedBy]] = (
            asyncio.get_running_loop().create_future()
        )
        self.registry.hold(
            approval_id,
            _Pending(
                scope=self.scope,
                tool_name=tool_name,
                argument_digest=argument_digest,
                risk=risk,
                answer=answer,
            ),
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                return await answer
        except TimeoutError:
            # The caller enforces the same bound and will report the timeout;
            # saying so here as well is what makes the attribution "nobody
            # answered" rather than "something went wrong".
            return "deny", "timeout"
        finally:
            self.registry.release(approval_id)


__all__ = [
    "UNREPEATABLE_RISKS",
    "ApprovalNotPendingError",
    "ApprovalScope",
    "CodeApprovalRegistry",
    "PendingApproval",
    "SessionApprovalGate",
    "StandingApprovalRefusedError",
]
