"""What this deployment cannot do, said out loud (ADR-102).

**Why a route rather than a log line.** Every fact on this page was already
decided at assembly and already recorded -- ``chat_unavailable``,
``rag_unavailable`` and the Task authorization envelope are fields on the
objects the routes already hold. What was missing is a reader. A capability
that is absent announces itself today in one of three ways, and a person using
the console sees none of them: a log line during startup, a tool the model
never mentions, and an answer that reads like the model is broken. The console
that made someone ask "是不是 key 失效了" was in fact a console whose Task
envelope was empty and whose Chat had no ``web_search`` -- a start-up decision,
visible nowhere.

**Names and states, never addresses or values.** ``/health/ready`` deliberately
returns no reason, because it is an orchestrator probe and the reason describes
the deployment's internals. This route does return reasons, under ADR-101's
premise rather than against it: the same port already hands out
``/v1/settings/provider-key``, which reports whether a key exists, its last four
characters and the *path* it lives at. What this route will not do is name an
address, a DSN, a URL or a credential -- an MCP server is reported by alias and
by the tool names its Tasks may call, never by endpoint. The rule is written
here because the next row somebody adds is where it gets broken.

**Three states, and the third is not a rounding of the other two.**
``unknown`` is what this process must answer about anything living in another
process. The API cannot see whether a Task Worker is running, and it certainly
cannot see whether that Worker was started with ``--demo`` -- there is no
Worker-to-control-plane reporting channel in this system (docs/known-gaps.md,
E-09). Reporting that absence as ``absent`` would be a claim; reporting it as
``available`` because the API can accept a submission would be worse. It is
unknown, and the row says which command answers it.

**The envelope, not a guess.** The Task rows are read off
``config.task.default_authorization_envelope`` -- the exact tuple that will be
frozen into the next Task at submission. So this page cannot drift from what a
Task actually gets authorized to do; it is the same value, not a second
derivation of the same configuration. What it still cannot promise is that the
Worker *registered* the tool the envelope allows: `profile_with_dynamic_tools`
narrows to what a Worker holds, and that is the other process again.

**Two quality notes are deliberately not rows here.** ``reranker_unavailable``
and ``sparse_unavailable`` describe how well retrieval works, not whether it
exists. A page that listed them beside a missing capability would teach its
reader that a downgrade and an absence are the same kind of fact.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME as EXPORT_ARTIFACT_TOOL,
)
from agent_workbench.adapters.tools.external_search import (
    TOOL_NAME as EXTERNAL_SEARCH_TOOL,
)
from agent_workbench.apps.api.dependencies import ApiDependencies
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.agents import DELEGATE_TOOL
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from agent_workbench.domain.workspace import (
    WORKSPACE_EDIT_TOOL,
    WORKSPACE_GREP_TOOL,
    WORKSPACE_LIST_TOOL,
    WORKSPACE_READ_TOOL,
    WORKSPACE_WRITE_TOOL,
)

SYSTEM_PREFIX: Final[str] = "/v1/system"

#: Everything a Task envelope can carry that did **not** come from an MCP
#: server. The MCP row is the remainder, which is exact by construction --
#: `task_authorization_envelope` builds the tuple out of these names plus
#: `mcp_tools` -- and stays exact only while this set is maintained. A new
#: built-in tool that is not listed here would be reported to the console as an
#: MCP tool, which is why `test_system_capabilities.py` pins the remainder
#: against a real envelope rather than against this set.
_BUILT_IN_TASK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        EXPORT_ARTIFACT_TOOL,
        EXTERNAL_SEARCH_TOOL,
        SANDBOX_RUN_TOOL,
        DELEGATE_TOOL,
        WORKSPACE_EDIT_TOOL,
        WORKSPACE_GREP_TOOL,
        WORKSPACE_LIST_TOOL,
        WORKSPACE_READ_TOOL,
        WORKSPACE_WRITE_TOOL,
    }
)

#: Said in two rows, so it is written once. Neither of them can be fixed from
#: inside a running deployment -- both need a different image.
_EMBEDDING_REMEDY: Final[str] = (
    "需要装了 embedding extra 的镜像与可用的 Qdrant；Compose 默认镜像不含它，"
    "那是一个几 GB 的可选依赖。"
)

router = APIRouter(prefix=SYSTEM_PREFIX, tags=["system"])


class Capability(BaseModel):
    """One thing this deployment can or cannot do, and why."""

    #: Stable across releases and safe to branch on. The console keys its rows
    #: by this and never by the title.
    id: str
    #: What to call it on a page somebody reads. Chinese, like every other
    #: human-facing string this API returns (`settings.restart_hint`), because
    #: the reason and the fact that produced it belong in one place rather than
    #: in a mapping the browser keeps in sync by hand.
    title: str
    #: `core` is what this product claims to be; `optional` is what it can be
    #: asked to also do. A deployment missing a core row is not a smaller
    #: console, it is a console with a piece of its front half removed --
    #: which is exactly the distinction somebody reading a broken stack needs
    #: first, and the one no log line was drawing.
    tier: Literal["core", "optional"]
    state: Literal["available", "absent", "unknown"]
    #: Empty when available. Otherwise the deployment's own recorded sentence
    #: where one exists, and never a sentence invented here to sound like one.
    reason: str = ""
    #: What would change it, in the words of whoever has to type it. Empty when
    #: nothing this side can do would help.
    remedy: str = ""
    #: Names, when a row has any worth naming -- the MCP tools a Task may call.
    #: Never an address.
    detail: tuple[str, ...] = ()


class DeploymentCapabilitiesResponse(BaseModel):
    """The whole report, as one list rather than a nested shape.

    Flat because every consumer so far wants to render it in tier order, and a
    grouping baked into the payload is a grouping the next reader has to undo.
    """

    capabilities: tuple[Capability, ...]


@router.get("/capabilities", response_model=DeploymentCapabilitiesResponse)
async def capabilities(request: Request) -> DeploymentCapabilitiesResponse:
    """Everything this process knows about what it can and cannot do."""

    dependencies = dependencies_of(request)
    # Resolved and discarded, as `tasks.capabilities`, `computer.session` and
    # `settings` all do. Nothing here is anybody's data -- it is this process
    # describing itself -- but a route that skipped the identity adapter would
    # be the one route in the API a caller could reach without one.
    dependencies.principals.resolve(request)
    config = dependencies.config
    allowed = tuple(
        str(name) for name in config.task.default_authorization_envelope.allowed_tools
    )
    mcp_tools = tuple(name for name in allowed if name not in _BUILT_IN_TASK_TOOLS)
    web_search_available = dependencies.serves_chat and config.research is not None

    rows: list[Capability] = [
        Capability(
            id="chat.direct",
            title="直接对话",
            tier="core",
            state="available" if dependencies.serves_chat else "absent",
            reason=""
            if dependencies.serves_chat
            else (dependencies.chat_unavailable or ""),
            remedy=(
                ""
                if dependencies.serves_chat
                else "在「系统」页保存 Provider Key，然后重启 API 进程。"
            ),
        ),
        Capability(
            id="chat.knowledge_base",
            title="知识库问答（RAG）",
            tier="core",
            state="available" if dependencies.rag_unavailable is None else "absent",
            # Not `rag_unavailable` forwarded blindly, and the difference showed
            # up the first time this ran against a real Compose stack. With no
            # provider key the whole of Chat fails to assemble, and assembly
            # records the *model* error into `rag_unavailable` too -- so this row
            # read "no API key" on a deployment where the true answer is "this
            # image has no embedding runtime, and a key would not change it".
            # A reason that names the wrong cause is worse than no reason: it
            # sends somebody to buy credit for a feature the image cannot run.
            reason=(
                ""
                if dependencies.rag_unavailable is None
                else _rag_reason(dependencies)
            ),
            remedy=(
                ""
                if dependencies.rag_unavailable is None
                else _EMBEDDING_REMEDY
                if not dependencies.serves_search
                else "在「系统」页保存 Provider Key，然后重启 API 进程。"
                if not dependencies.serves_chat
                else ""
            ),
        ),
        Capability(
            id="knowledge.search",
            title="知识库检索（不含模型）",
            tier="core",
            state="available" if dependencies.serves_search else "absent",
            reason=(
                ""
                if dependencies.serves_search
                else "这个进程没有装配检索，上传的文档无法被搜索。"
            ),
            # The same sentence as the row above, spelled out rather than
            # "同上": these rows are grouped by tier and a reader can meet this
            # one first, at which point a back-reference points at nothing.
            remedy="" if dependencies.serves_search else _EMBEDDING_REMEDY,
        ),
        Capability(
            id="task.submit",
            title="提交任务",
            tier="core",
            # Unconditional by assembly: `task_service` is not optional on
            # `ApiDependencies`, so this row is `available` in every process
            # that serves routes at all. It is here so the two halves of Task
            # -- may I submit one, and will anybody run it -- are two rows a
            # reader can see disagree.
            state="available",
        ),
        Capability(
            id="task.worker",
            title="任务 Worker",
            tier="core",
            # See the module docstring: this process has no channel through
            # which a Worker reports itself, so it cannot tell "no Worker" from
            # "a Worker started with --demo" from "a real Worker". None of the
            # three is worth guessing at.
            state="unknown",
            reason=(
                "本部署没有 Worker 上报通道：从 API 看不出有没有 Worker 在跑，"
                "也看不出它是真实 Worker 还是 --demo 合成 Worker。"
            ),
            remedy=(
                "docker compose --profile demo ps 看进程，"
                "docker compose logs task-worker 看它启动时注册了哪些图与工具。"
            ),
        ),
        Capability(
            id="chat.web_search",
            title="对话联网搜索",
            tier="optional",
            # Both halves, because a tool needs a turn to live in: a process
            # assembled `--without-chat` can have `[research]` configured and
            # still offer nobody a search. Reading only the config would report
            # a capability with no caller.
            state="available" if web_search_available else "absent",
            reason=(
                ""
                if web_search_available
                else (
                    "没有配置 [research]，所以这个进程根本没造过 web_search 这件工具"
                    "——模型不是拒绝联网，是手上没有它。"
                )
                if dependencies.serves_chat
                else "这个进程没有装配 Chat，联网搜索没有可以寄身的回合。"
            ),
            remedy=(
                ""
                if web_search_available
                else (
                    "先有 Provider Key，再让 API 带 AW_RESEARCH__ENABLED=true 启动。"
                    "没有 key 时这个开关会让进程直接拒绝启动，所以两者必须同时具备。"
                )
            ),
        ),
        Capability(
            id="code.sessions",
            title="Code 会话",
            tier="optional",
            state="available" if dependencies.serves_code else "absent",
            reason=(
                ""
                if dependencies.serves_code
                else "这份配置里 code.enabled 为假。"
                if not config.code.enabled
                else (
                    "code.enabled 为真，但这个进程没能装配模型："
                    "编码回合是一个模型循环，或者什么都不是。"
                )
            ),
            remedy=""
            if dependencies.serves_code
            else "配置 [code] enabled 并提供 Provider Key。",
        ),
        Capability(
            id="task.external_search",
            title="任务联网搜索",
            tier="optional",
            state="available" if EXTERNAL_SEARCH_TOOL in allowed else "absent",
            reason=(
                ""
                if EXTERNAL_SEARCH_TOOL in allowed
                else (
                    "下一个任务的授权信封里没有 external_search，"
                    "研究节点提议它只会被自己的信封拒绝。"
                )
            ),
            remedy=(
                ""
                if EXTERNAL_SEARCH_TOOL in allowed
                else (
                    "API 与 Worker 都要带 AW_RESEARCH__ENABLED=true 启动；"
                    "信封在提交那一刻冻结。"
                )
            ),
        ),
        Capability(
            id="task.mcp_tools",
            title="任务可用的 MCP 工具",
            tier="optional",
            state="available" if mcp_tools else "absent",
            detail=mcp_tools,
            reason=""
            if mcp_tools
            else "配置里没有 [[mcp.servers]]，所以任务信封里一件 MCP 工具都没有。",
            remedy=(
                ""
                if mcp_tools
                else (
                    "启动 Word/web MCP 服务，并用声明了它们的配置档启动 API 与 Worker"
                    "（scripts/dev.sh demo-api / demo-worker）。"
                )
            ),
        ),
        Capability(
            id="task.sandbox",
            title="任务沙箱执行",
            tier="optional",
            state="available" if SANDBOX_RUN_TOOL in allowed else "absent",
            reason=""
            if SANDBOX_RUN_TOOL in allowed
            else "沙箱未启用，任务信封里没有 sandbox_run。",
            remedy=(
                ""
                if SANDBOX_RUN_TOOL in allowed
                else (
                    "启动 sandbox MCP 服务（它需要能创建容器），"
                    "并在配置里打开 [sandbox]。"
                )
            ),
        ),
        Capability(
            id="task.delegation",
            title="子代理委派",
            tier="optional",
            state="available" if DELEGATE_TOOL in allowed else "absent",
            reason=""
            if DELEGATE_TOOL in allowed
            else "multi_agent.delegation_enabled 为假。",
            remedy=""
            if DELEGATE_TOOL in allowed
            else "在配置里打开 [multi_agent] delegation_enabled。",
        ),
        Capability(
            id="task.triage",
            title="任务分流（自动选图）",
            tier="optional",
            state="available" if dependencies.triage is not None else "absent",
            reason=(
                ""
                if dependencies.triage is not None
                else "triage 未启用或没有模型，/v1/tasks/triage 会回答 default。"
            ),
            remedy=(
                ""
                if dependencies.triage is not None
                else "打开 [triage] enabled 并提供 Provider Key。"
            ),
        ),
    ]
    return DeploymentCapabilitiesResponse(capabilities=tuple(rows))


def _rag_reason(dependencies: ApiDependencies) -> str:
    """Which of the two halves is missing, asked in the order that decides.

    Retrieval first: it is the half a provider key cannot buy. Only when this
    process *does* hold a retrieval runtime is "there is no Chat to ground"
    the operative sentence, and only when it holds both is the recorded
    `rag_unavailable` sentence about something else (a deployment that serves
    `chat.retrieval_shape = "ungrounded"` on purpose) -- which is the one case
    where forwarding it verbatim is right.
    """

    if not dependencies.serves_search:
        return (
            "这个进程没有装配检索运行时：镜像里没有 embedding extra，"
            "或者 Qdrant 不可用。有没有 Provider Key 都改变不了这一条。"
        )
    if not dependencies.serves_chat:
        return f"检索装配起来了，但这个进程没有 Chat：{dependencies.chat_unavailable}"
    return dependencies.rag_unavailable or ""


__all__ = [
    "SYSTEM_PREFIX",
    "Capability",
    "DeploymentCapabilitiesResponse",
    "router",
]
