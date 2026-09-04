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

**One write, and what it writes is a wish (ADR-103).** ``PUT /switches/{id}``
records that the *next* start should assemble an optional part; nothing in
this process changes. ADR-102 §4 had refused any write here on the ground that
a console which can change capabilities has no single answer to "what is this
deployment" -- and the answer that keeps it single is the one ADR-101 found for
the key: the file is for the next start, the report says stored and running
as two fields, and an operator's environment still wins. Only switch-shaped
parts get a switch. A part that needs a server, a socket or another image
says ``install``, because a switch for it would be a promise this side cannot
keep.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from agent_workbench.adapters.documents.fidelity import find_soffice
from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME as EXPORT_ARTIFACT_TOOL,
)
from agent_workbench.adapters.tools.external_search import (
    TOOL_NAME as EXTERNAL_SEARCH_TOOL,
)
from agent_workbench.application.switches import SwitchRefused, spec_for
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

#: The four switch ids, spelled once. They are settings paths, and the row
#: that carries one is the row that setting assembles.
RESEARCH_SWITCH: Final[str] = "research.enabled"
TRIAGE_SWITCH: Final[str] = "triage.enabled"
CODE_SWITCH: Final[str] = "code.enabled"
DELEGATION_SWITCH: Final[str] = "multi_agent.delegation_enabled"

#: Same words as the key's hint, because it is the same fact: these processes
#: read their configuration once.
_RESTART_HINT: Final[str] = "重启 agent-api 与 agent-task-worker 后这个选择才会生效。"
_NEEDS_MODEL: Final[str] = (
    "这一项要有模型才装配得起来：先在「模型密钥」里存一把 key，再重启。"
)

#: Whether this process can lay a Word document out (ADR-0045), asked the
#: way the artifact route asks it. A module attribute rather than a direct
#: call so the tests can pin the answer: `find_soffice` reads `PATH` and one
#: macOS bundle path, and a row whose state depended on what the test
#: machine happens to have installed would be green on one laptop and red on
#: the next.
layout_converter = find_soffice

router = APIRouter(prefix=SYSTEM_PREFIX, tags=["system"])


class CapabilitySwitch(BaseModel):
    """A part the console may switch, and the two answers about it.

    ``stored`` is what the console's file says for the next start -- ``None``
    when it says nothing, which is a state: the start then follows the
    environment and the configuration files. ``active`` is what *this* process
    runs with. They are two fields because they are two questions, and a page
    that merged them would claim a switch flipped a second ago is in effect.
    """

    #: The settings path this switch moves, e.g. ``research.enabled``. Two
    #: rows may share one (Chat's and the Task's web search are one setting).
    id: str
    stored: bool | None
    active: bool
    #: True when the file changed since this process read it.
    restart_required: bool
    restart_hint: str = ""
    #: Non-empty when the file said "on" at start and the loader deliberately
    #: did not apply it -- ``research.enabled`` with no key -- with the reason.
    held: str = ""
    #: True when the file said something at start and the process runs with
    #: the opposite for a reason other than ``held``: an exported variable or
    #: a higher source decided. Flipping the switch here cannot change that.
    overridden: bool = False
    #: Whether "on" only assembles with a provider key present.
    needs_model: bool = False
    #: Why "on" would not take effect right now, when that is already known.
    #: Never a reason to refuse the write: the file records a wish.
    blocked: str = ""


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
    #: How this part is provided (ADR-103). ``switch``: a stored boolean is all
    #: it takes, and ``switch`` below is that boolean. ``install``: a server,
    #: a socket or another image -- nothing on this page can supply it.
    #: ``key``: only the provider key, which has its own panel. ``none``:
    #: nothing to provide.
    provision: Literal["switch", "install", "key", "none"] = "none"
    switch: CapabilitySwitch | None = None


class DeploymentCapabilitiesResponse(BaseModel):
    """The whole report, as one list rather than a nested shape.

    Flat because every consumer so far wants to render it in tier order, and a
    grouping baked into the payload is a grouping the next reader has to undo.
    """

    capabilities: tuple[Capability, ...]


class SwitchRequest(BaseModel):
    """The choice, and nothing else that could be smuggled alongside it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get("/capabilities", response_model=DeploymentCapabilitiesResponse)
async def capabilities(request: Request) -> DeploymentCapabilitiesResponse:
    """Everything this process knows about what it can and cannot do."""

    dependencies = dependencies_of(request)
    # Resolved and discarded, as `tasks.capabilities`, `computer.session` and
    # `settings` all do. Nothing here is anybody's data -- it is this process
    # describing itself -- but a route that skipped the identity adapter would
    # be the one route in the API a caller could reach without one.
    dependencies.principals.resolve(request)
    return _capabilities(dependencies)


@router.put("/switches/{switch_id}", response_model=DeploymentCapabilitiesResponse)
async def store_switch(
    switch_id: str, request: Request, body: SwitchRequest
) -> DeploymentCapabilitiesResponse | JSONResponse:
    """Record what the next start should do about one optional part.

    Answers with the whole report rather than the one switch, because the row
    a switch moves is what the person is looking at, and two rows may share
    one switch. Nothing in this process changes; the report says so in
    ``restart_required``.
    """

    dependencies = dependencies_of(request)
    dependencies.principals.resolve(request)
    if spec_for(switch_id) is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"没有叫 {switch_id!r} 的开关"},
        )
    try:
        dependencies.switches.set(switch_id, body.enabled)
    except SwitchRefused as refused:
        # 400 for the reason the key route gives: every refusal is about the
        # request or a deployment choice the caller can see, in a sentence.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(refused)}
        )
    return _capabilities(dependencies)


@router.delete("/switches/{switch_id}", response_model=DeploymentCapabilitiesResponse)
async def withdraw_switch(
    switch_id: str, request: Request
) -> DeploymentCapabilitiesResponse | JSONResponse:
    """Take the console's choice back, so the next start follows the environment."""

    dependencies = dependencies_of(request)
    dependencies.principals.resolve(request)
    if spec_for(switch_id) is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"没有叫 {switch_id!r} 的开关"},
        )
    try:
        dependencies.switches.set(switch_id, None)
    except SwitchRefused as refused:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(refused)}
        )
    return _capabilities(dependencies)


def _switch_views(dependencies: ApiDependencies) -> dict[str, CapabilitySwitch]:
    """Every switch, as the file says now against what the process loaded."""

    try:
        stored_now = dependencies.switches.read()
        unreadable = ""
    except SwitchRefused as refused:
        # The page must not die on the file it exists to explain. Every switch
        # reads as undecided and says why, and nothing is called "owed".
        stored_now = {}
        unreadable = f"读不到已存的开关：{refused}"
    active_key = dependencies.config.model.api_key is not None
    stored_key = dependencies.provider_keys.status(active_key=None).stored
    model_present = active_key or stored_key

    views: dict[str, CapabilitySwitch] = {}
    for state in dependencies.config.switches:
        spec = spec_for(state.path)
        if spec is None:  # pragma: no cover - the projection iterates SWITCHES
            continue
        stored = stored_now.get(state.path)
        restart_required = not unreadable and stored != state.stored_at_start
        views[state.path] = CapabilitySwitch(
            id=state.path,
            stored=stored,
            active=state.active,
            restart_required=restart_required,
            restart_hint=_RESTART_HINT if restart_required else "",
            held=state.held,
            overridden=(
                state.stored_at_start is not None
                and state.stored_at_start != state.active
                and not state.held
            ),
            needs_model=spec.needs_model,
            blocked=unreadable
            or (_NEEDS_MODEL if spec.needs_model and not model_present else ""),
        )
    return views


def _capabilities(dependencies: ApiDependencies) -> DeploymentCapabilitiesResponse:
    config = dependencies.config
    allowed = tuple(
        str(name) for name in config.task.default_authorization_envelope.allowed_tools
    )
    mcp_tools = tuple(name for name in allowed if name not in _BUILT_IN_TASK_TOOLS)
    web_search_available = dependencies.serves_chat and config.research is not None
    # Each and-ed with `serves_code`: a flag that is on in a process with no
    # Code is a row about a session that cannot exist.
    code_sandbox = dependencies.serves_code and config.code.sandbox_enabled
    code_host_commands = dependencies.serves_code and config.code.host_commands_enabled
    code_web_search = dependencies.serves_code and config.code.web_search_enabled
    layout_available = layout_converter() is not None
    switches = _switch_views(dependencies)
    research_held = (
        switches[RESEARCH_SWITCH].held if RESEARCH_SWITCH in switches else ""
    )

    rows = [
        Capability(
            id="chat.direct",
            title="直接对话",
            tier="core",
            state="available" if dependencies.serves_chat else "absent",
            reason=dependencies.chat_unavailable or "",
            remedy=(
                ""
                if dependencies.serves_chat
                else "在「系统」页保存 Provider Key，然后重启 API 进程。"
            ),
            provision="key",
        ),
        Capability(
            id="chat.knowledge_base",
            title="知识库问答（RAG）",
            tier="core",
            state="available" if dependencies.rag_unavailable is None else "absent",
            reason=""
            if dependencies.rag_unavailable is None
            else _rag_reason(dependencies),
            remedy=(
                ""
                if dependencies.rag_unavailable is None
                else _EMBEDDING_REMEDY
                if not dependencies.serves_search
                else "在「系统」页保存 Provider Key，然后重启 API 进程。"
                if not dependencies.serves_chat
                else ""
            ),
            provision="install" if not dependencies.serves_search else "key",
        ),
        Capability(
            id="knowledge.search",
            title="知识库检索（不含模型）",
            tier="core",
            state="available" if dependencies.serves_search else "absent",
            reason=(
                ""
                if dependencies.serves_search
                else "这个进程没有装配检索运行时（embedding extra 或 Qdrant 缺席）。"
            ),
            remedy="" if dependencies.serves_search else _EMBEDDING_REMEDY,
            provision="install",
        ),
        Capability(
            id="task.submit",
            title="提交任务",
            tier="core",
            # The registry and the graph version are assembled unconditionally;
            # the only thing submission needs from outside is PostgreSQL, and
            # /health/ready already answers for that.
            state="available",
        ),
        Capability(
            id="task.worker",
            title="任务 Worker",
            tier="core",
            # Not a rounding of the other two. The API cannot see another
            # process, and a Worker that is up, healthy and synthetic looks
            # exactly like one that is real from here.
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
                else research_held
                if research_held and dependencies.serves_chat
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
                    "打开这一行的开关（或让 API 带 AW_RESEARCH__ENABLED=true 启动），"
                    "有 Provider Key 之后重启。没有 key 时这个开关会被搁置而不是让进程"
                    "拒绝启动。"
                )
            ),
            provision="switch",
            switch=switches.get(RESEARCH_SWITCH),
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
            else "打开这一行的开关并提供 Provider Key，然后重启 API。",
            provision="switch",
            switch=switches.get(CODE_SWITCH),
        ),
        # The three things a coding session may reach beyond its files, one
        # row each (ADR-0109). Reported separately from `code.sessions`
        # because they fail separately and are repaired separately: a Windows
        # console had Code on and every one of these off, and the only place
        # that said so was the model's own "本环境没有 shell 与网络" -- a
        # sentence that reads as a lazy model rather than as a fact about the
        # deployment. Each row says which it is.
        Capability(
            id="code.sandbox",
            title="编码会话的沙箱运行（sandbox_run）",
            tier="optional",
            state="available" if code_sandbox else "absent",
            reason=(
                ""
                if code_sandbox
                else "Code 会话没装配起来，沙箱无从谈起。"
                if not dependencies.serves_code
                else (
                    "这个进程启动时沙箱没有打开：code.sandbox_enabled 为假，"
                    "或 [sandbox] 没有应答。不挂项目的会话因此没有 sandbox_run。"
                )
            ),
            remedy=(
                ""
                if code_sandbox
                else (
                    "Compose 栈里它是 `sandbox` 容器（ADR-0107）：API 启动时探它的"
                    "运行时，探到才打开；看 docker compose logs sandbox，然后"
                    " restart。原生路径：scripts/dev.sh sandbox-server。"
                )
            ),
            provision="install",
        ),
        Capability(
            id="code.host_commands",
            title="编码会话的宿主命令（project_run）",
            tier="optional",
            state="available" if code_host_commands else "absent",
            reason=(
                ""
                if code_host_commands
                else "Code 会话没装配起来。"
                if not dependencies.serves_code
                else (
                    "policy.shell_tools_enabled 为假：项目会话只有读写文件的五件"
                    "工具，没有 shell，也开不了这台机器上的浏览器。"
                )
            ),
            remedy=(
                ""
                if code_host_commands
                else (
                    "这是宿主机自己的 shell（ADR-077），只有 API 跑在你机器上的"
                    "原生路径（scripts/dev.sh up）打开它。容器里没有这台机器的"
                    " shell，Compose 栈不提供（ADR-0109）。"
                )
            ),
            provision="install",
        ),
        Capability(
            id="code.web_search",
            title="编码会话联网搜索",
            tier="optional",
            state="available" if code_web_search else "absent",
            reason=(
                ""
                if code_web_search
                else "Code 会话没装配起来。"
                if not dependencies.serves_code
                else "policy.search_tools_enabled 为假。"
                if not config.code.search_tools_enabled
                else research_held
                if research_held
                else "没有搜索 provider：「联网搜索」开关是关的，或没有 key。"
            ),
            remedy=(
                ""
                if code_web_search
                else "在配置档的 [policy] 里打开 search_tools_enabled，然后重启 API。"
                if dependencies.serves_code and not config.code.search_tools_enabled
                else (
                    "打开「联网搜索」开关——它和「对话联网搜索」是同一个——然后重启 API。"
                )
            ),
            # A switch only where flipping it can help. With the policy flag
            # off, the research switch would light up and change nothing.
            provision="switch" if config.code.search_tools_enabled else "install",
            switch=(
                switches.get(RESEARCH_SWITCH)
                if config.code.search_tools_enabled
                else None
            ),
        ),
        Capability(
            id="artifact.layout_preview",
            title="Word 版面预览",
            tier="optional",
            state="available" if layout_available else "absent",
            reason=(
                ""
                if layout_available
                else (
                    "这个进程找不到 LibreOffice（soffice）：.docx 只有文字预览，"
                    "没有版面。"
                )
            ),
            remedy=(
                ""
                if layout_available
                else (
                    "Compose：用 scripts\\stack.cmd 重新构建镜像，它自 ADR-0109 起"
                    "默认带 LibreOffice（`lite` 不带）。原生路径：安装 LibreOffice，"
                    "让 soffice 在 PATH 上。"
                )
            ),
            provision="install",
        ),
        Capability(
            id="task.external_search",
            title="任务联网搜索",
            tier="optional",
            state="available" if EXTERNAL_SEARCH_TOOL in allowed else "absent",
            reason=(
                ""
                if EXTERNAL_SEARCH_TOOL in allowed
                else research_held
                if research_held
                else (
                    "下一个任务的授权信封里没有 external_search，"
                    "研究节点提议它只会被自己的信封拒绝。"
                )
            ),
            remedy=(
                ""
                if EXTERNAL_SEARCH_TOOL in allowed
                else (
                    "打开「联网搜索」开关——它和「对话联网搜索」是同一个——"
                    "然后重启 API 与 Worker；信封在提交那一刻冻结。"
                )
            ),
            provision="switch",
            switch=switches.get(RESEARCH_SWITCH),
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
                    "Compose 栈自 ADR-0105 起在每个 Worker 容器里以 sidecar "
                    "起这两个服务。"
                )
            ),
            provision="install",
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
                    "并在配置里打开 [sandbox]。Compose 栈里它是 `sandbox` 容器"
                    "（ADR-0107）：API 与 Worker 启动时探它的运行时，探到才打开；"
                    "看 docker compose logs sandbox，然后 restart。"
                )
            ),
            provision="install",
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
            else "打开这一行的开关，然后重启 API 与 Worker。",
            provision="switch",
            switch=switches.get(DELEGATION_SWITCH),
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
                else "打开这一行的开关并提供 Provider Key，然后重启 API。"
            ),
            provision="switch",
            switch=switches.get(TRIAGE_SWITCH),
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
    "CODE_SWITCH",
    "DELEGATION_SWITCH",
    "RESEARCH_SWITCH",
    "SYSTEM_PREFIX",
    "TRIAGE_SWITCH",
    "Capability",
    "CapabilitySwitch",
    "DeploymentCapabilitiesResponse",
    "SwitchRequest",
    "router",
]
