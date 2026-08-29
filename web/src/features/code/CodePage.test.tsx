import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  deleteCodeSession,
  downloadCodeWorkspaceFile,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
  listCodeSessions,
  listProjectFiles,
  putCodeWorkspaceFile,
  readProjectFile,
  renameCodeSession,
  runCodeWorkspaceFile,
} from "../../api/client";
import type * as ApiClient from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { CodePage } from "./CodePage";
import { DOCX_MEDIA_TYPE } from "../../components/media";
import { useCodeStream } from "./useCodeStream";

vi.mock("../../api/client", async () => ({
  // Two real exports among the mocks, for the same reason: a component under
  // test reads them as values rather than calling them, so a factory that
  // omits them breaks on render or on the first failed run.
  //
  // `ApiError` has to be the real class because `remedyFor` asks
  // `cause instanceof ApiError` to decide which sentence a refused run gets.
  // Left out, that check throws on `instanceof undefined` -- and only on the
  // failure path, so the file would stay green until somebody wrote the first
  // test of a run that did not happen.
  ApiError: (await vi.importActual<typeof ApiClient>("../../api/client"))
    .ApiError,
  // Read by HtmlPreview to size-gate before fetching.
  MAX_PREVIEW_BYTES: 512 * 1024,
  askCode: vi.fn(),
  createCodeSession: vi.fn(),
  decideCodeApproval: vi.fn(),
  deleteCodeSession: vi.fn(() => Promise.resolve({ session_id: "ses_code_1" })),
  getCodeApprovals: vi.fn(() => Promise.resolve({ approvals: [] })),
  getCodeHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  getCodeWorkspace: vi.fn(() => Promise.resolve({ files: [] })),
  getCodeWorkspaceFileText: vi.fn(() =>
    Promise.resolve({ text: "", truncated: false }),
  ),
  getCodeWorkspaceFileBlob: vi.fn(() =>
    Promise.resolve(
      new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" }),
    ),
  ),
  downloadCodeWorkspaceFile: vi.fn(() => Promise.resolve()),
  listCodeSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  putCodeWorkspaceFile: vi.fn(() => Promise.resolve({ files: [] })),
  renameCodeSession: vi.fn(() =>
    Promise.resolve({
      session_id: "ses_code_1",
      title: "x",
      last_activity_at: null,
      project_id: null,
    }),
  ),
  runCodeWorkspaceFile: vi.fn(),
  newIdempotencyKey: vi.fn(() => "code-1"),
  // ADR-074. Code 现在有一道门：没选文件夹就没有起始屏。这些是那道门读的东西，
  // 默认给一个已经有目录的项目，好让绝大多数用例只需要多点一下就回到原来的形状。
  listProjects: vi.fn(() => Promise.resolve({ projects: [PROJECT] })),
  getProject: vi.fn(() => Promise.resolve(PROJECT)),
  createProjectAtDirectory: vi.fn(() => Promise.resolve(PROJECT)),
  browseDirectories: vi.fn(() =>
    Promise.resolve({
      path: "/Users/alice",
      parent: "/Users",
      entries: [{ name: "demo", path: "/Users/alice/demo" }],
      truncated: false,
    }),
  ),
  setCodeSessionProject: vi.fn(() => Promise.resolve()),
  listProjectFiles: vi.fn(() =>
    Promise.resolve({ path: "", entries: [], truncated: false }),
  ),
  readProjectFile: vi.fn(() =>
    Promise.resolve({
      path: "a.txt",
      text: "",
      size_bytes: 0,
      is_text: true,
      modified_at: "2026-08-22T00:00:00Z",
    }),
  ),
}));

const PROJECT = {
  project_id: "prj_1",
  name: "demo",
  created_at: "2026-08-22T00:00:00Z",
  updated_at: "2026-08-22T00:00:00Z",
  archived_at: null,
  root_path: "/Users/alice/demo",
};

// The stream opens a real `fetch` against an SSE endpoint. What it delivers is
// asserted through this seam instead, because a page test that waited on a
// network read would be testing the transport a second time.
vi.mock("./useCodeStream", () => ({
  useCodeStream: vi.fn(() => ({
    steps: [],
    thinking: "",
    thinkingCallId: "",
    answer: "",
    progress: new Map(),
  })),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: vi.fn(),
}));

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["workspace:write"],
};

const BOB: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "bob",
  scopes: ["workspace:write"],
};

const SESSION = "ses_code_1";

/**
 * 走过 ADR-074 那道门。
 *
 * 每个从 `/code`（没有会话）开始的用例都要先选一个文件夹，因为在那之前根本没有
 * 输入框。写成辅助函数而不是在每个用例里点一遍，是为了让这些用例断言的仍然是
 * 它们各自那件事——而不是每一条都顺带再测一遍这道门。门本身由它自己的用例测。
 */
async function chooseFolder(user: ReturnType<typeof userEvent.setup>) {
  // 限定在选择器那一列里。一个裸的 `/demo/` 会同时匹配到侧栏的会话行——它现在
  // 在「全部会话」那一档下也说自己属于哪个文件夹，而起始屏正是那一档（还没有
  // 打开的会话，就没有 `currentProjectId` 去收窄）。
  const chooser = (await screen.findByRole("heading", {
    name: "在哪个文件夹里编码？",
  })).closest("div.aw-code-chooser") as HTMLElement;
  await user.click(await within(chooser).findByRole("button", { name: /demo/ }));
}

function mounted(entry: string = `/code/${SESSION}`) {
  // A client per render, with retries off: a shared one would carry one test's
  // session list into the next, and a retry would turn "the list failed" into
  // a test that hangs rather than one that fails.
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          {/* The same optional-param route App.tsx mounts, so the first send's
              /code → /code/:id navigation stays inside one component instance
              here too. */}
          <Route element={<CodePage />} path="/code/:sessionId?" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Let one animation frame pass.
 *
 * Focus is handed back from inside `requestAnimationFrame`, so an assertion
 * that focus was *not* taken proves nothing until a frame has run -- the
 * unguarded version of that code passes such a test simply by being late.
 */
async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => {
      window.requestAnimationFrame(() => {
        resolve(undefined);
      });
    });
  });
}

/**
 * Open the right column on the full listing.
 *
 * A file is reached in two clicks now rather than one, and that is the trade
 * this layout makes on purpose: the column no longer mounts itself the moment
 * a session has any file at all, so the conversation keeps the width until
 * somebody asks for it. Produced files skip this entirely -- they are cards in
 * the turn that made them.
 */
async function openWorkspace(
  user: ReturnType<typeof userEvent.setup>,
  count: number,
) {
  await user.click(
    await screen.findByRole("button", { name: `工作区 ${String(count)}` }),
  );
}

beforeEach(() => {
  // 预览栏的展开状态存在 localStorage 里（`aw.code.panel.v1`），而 localStorage
  // 是整个文件共用的一份。不清掉的话，一个用例点开预览栏，下一个用例一进来它
  // 就是开着的——于是那句 `点「工作区 N」`，做的事从「打开」变成了「收起」。
  localStorage.clear();
  // Call counts are what two of these tests assert on, and a mock is shared by
  // the whole file: without this, "was never called" means "was not called
  // since the last test that happened to reset it".
  vi.clearAllMocks();
  vi.mocked(useIdentity).mockReturnValue({
    identity: ALICE,
    setIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  } as unknown as ReturnType<typeof useIdentity>);
  vi.mocked(getCodeHistory).mockResolvedValue({ messages: [] });
  vi.mocked(getCodeApprovals).mockResolvedValue({ approvals: [] });
  vi.mocked(getCodeWorkspace).mockResolvedValue({ files: [] });
  vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
    text: "",
    truncated: false,
  });
  vi.mocked(downloadCodeWorkspaceFile).mockResolvedValue(undefined);
  vi.mocked(listCodeSessions).mockResolvedValue({ sessions: [] });
  vi.mocked(useCodeStream).mockReturnValue({
    progress: new Map(),
    steps: [],
    thinking: "",
    thinkingCallId: "",
    answer: "",
  });
});

describe("CodePage 的计划模式", () => {
  // ADR-0079。开关要真的改变**发出去的那一轮**，不是只改一个图标：模式在回合起始
  // 被冻进信封，所以「界面显示计划中」和「服务端跑的是计划」必须是同一件事。
  it("把开关的状态发给服务端，而不是只改自己的样子", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "我会改三个文件。",
      workspace_version: null,
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted();
    await user.click(screen.getByRole("button", { name: "只做计划" }));
    await user.type(screen.getByLabelText("要做的事"), "加个功能");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(vi.mocked(askCode)).toHaveBeenCalled();
    });
    expect(vi.mocked(askCode).mock.calls[0]?.[4]).toBe("plan");
    // 计划档不加写入闸：这一轮根本没有写工具，给它一道「写入前问我」是在描述
    // 一个它不在的世界（`code_session.py` 的 `_system_prompt_for`）。
    expect(vi.mocked(askCode).mock.calls[0]?.[5]).toBe("standard");
  });

  it("默认是执行模式，并且把它明写在请求里", async () => {
    // 显式发 `"act"` 而不是省掉这个字段：服务端有默认值，但一个省略了字段的请求
    // 会让「这一轮是哪种模式」只能靠知道服务端默认值来回答——而要回答它的时候，
    // 手上往往只有那一条请求日志。
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "改好了。",
      workspace_version: null,
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "加个功能");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(vi.mocked(askCode)).toHaveBeenCalled();
    });
    expect(vi.mocked(askCode).mock.calls[0]?.[4]).toBe("act");
    expect(vi.mocked(askCode).mock.calls[0]?.[5]).toBe("standard");
  });

  it("选「改前问我」时，把写入闸随这一轮一起发出去", async () => {
    // 这一档是 ADR-087 的全部内容：同一批工具，换一个「谁来拍板」。所以断言
    // 的是两个字段一起——只看 approvals 会漏掉「它顺手把工具也收窄了」这种
    // 回归，而那会让这一档变成第二个计划模式。
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "改好了。",
      workspace_version: null,
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted();
    await user.click(screen.getByRole("button", { name: "改前问我" }));
    await user.type(screen.getByLabelText("要做的事"), "加个功能");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(vi.mocked(askCode)).toHaveBeenCalled();
    });
    expect(vi.mocked(askCode).mock.calls[0]?.[4]).toBe("act");
    expect(vi.mocked(askCode).mock.calls[0]?.[5]).toBe("before_write");
  });

  it("计划跑完之后给一个按钮，按下去是新的一轮 act", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "我会改三个文件。",
      workspace_version: null,
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted();
    await user.click(screen.getByRole("button", { name: "只做计划" }));
    await user.type(screen.getByLabelText("要做的事"), "加个功能");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const offer = await screen.findByRole("button", { name: "按这个计划执行" });
    await user.click(offer);

    await waitFor(() => {
      expect(vi.mocked(askCode).mock.calls).toHaveLength(2);
    });
    // 同一条指令，模式换成 act——重发的是请求，不是计划正文。计划是散文，它不
    // 授权任何东西，后面这一轮拿到的是它自己的信封。
    expect(vi.mocked(askCode).mock.calls[1]?.[2]).toBe("加个功能");
    expect(vi.mocked(askCode).mock.calls[1]?.[4]).toBe("act");
  });

  it("一轮 act 之后不再提议执行计划", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "改好了。",
      workspace_version: null,
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "加个功能");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(vi.mocked(askCode)).toHaveBeenCalled();
    });
    expect(
      screen.queryByRole("button", { name: "按这个计划执行" }),
    ).toBeNull();
  });
});

describe("CodePage 的多 agent 面板（ADR-089）", () => {
  function event(
    runId: string,
    eventType: string,
    sequence: number,
    payload: Record<string, unknown> = {},
  ) {
    return {
      schema_version: 1,
      event_id: `evt_${String(sequence)}`,
      stream_id: "ses_1",
      run_id: runId,
      event_type: eventType,
      durability: "durable" as const,
      timestamp: "2026-08-28T10:00:00Z",
      payload: { kind: eventType, ...payload },
      sequence,
      task_id: null,
      graph_node_id: null,
      parent_event_id: null,
    };
  }

  const delegated = [
    event("run_parent", "RunStarted", 1, {
      run_kind: "code",
      model_profile: "main",
      tool_names: ["project_read"],
      budget: {
        max_steps: 40,
        max_tool_calls: 32,
        max_total_tokens: null,
        max_cost_micro_usd: null,
        deadline: null,
      },
    }),
    event("run_parent", "AgentDelegated", 2, {
      child_agent_run_id: "run_child",
      profile_name: "explorer",
    }),
    event("run_child", "RunStarted", 3, {
      run_kind: "code",
      model_profile: "main",
      tool_names: ["project_read"],
      budget: {
        max_steps: 40,
        max_tool_calls: 32,
        max_total_tokens: null,
        max_cost_micro_usd: null,
        deadline: null,
      },
    }),
  ];

  it("一个会话真的委派过时，Code 页也画得出参与的 Agent", async () => {
    // 这条钉的正是用户反馈的那句「还是没有 agent 面板」：委派在 Code 里跑起来了，
    // 而面板只做在 Work 页，于是屏幕上什么都没有。
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: delegated,
      thinking: "",
      thinkingCallId: "",
      answer: "",
    });

    mounted();

    const panel = await screen.findByRole("region", {
      name: "参与这次任务的 Agent",
    });
    expect(within(panel).getByText("explorer")).toBeInTheDocument();
    expect(within(panel).getByText(/1 个是子代理/)).toBeInTheDocument();
  });

  it("带着 ?run= 进来时，收窄一开始就生效——这条链接发得出去也刷得回来", async () => {
    // 与 Work 页那条 `?run=` 同一个形状。断言的是**深链进来**而不是点击之后的
    // `window.location`：这里挂的是 `MemoryRouter`，它不动 window.location，而
    // 一条只在真实 router 下才成立的断言，测的是 router 不是这个页面。
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: delegated,
      thinking: "",
      thinkingCallId: "",
      answer: "",
    });

    mounted(`/code/${SESSION}?run=run_child`);

    const panel = await screen.findByRole("region", {
      name: "参与这次任务的 Agent",
    });
    // 那一行是被选中的，且面板给出了退出收窄的话与出口。
    expect(
      within(panel).getByRole("button", { name: /explorer/ }),
    ).toHaveAttribute("aria-current", "true");
    expect(within(panel).getByText(/只显示这一个运行/)).toBeInTheDocument();
  });

  it("点一行会收窄，再点一次退出", async () => {
    const user = userEvent.setup();
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: delegated,
      thinking: "",
      thinkingCallId: "",
      answer: "",
    });

    mounted();
    const panel = await screen.findByRole("region", {
      name: "参与这次任务的 Agent",
    });
    const row = within(panel).getByRole("button", { name: /explorer/ });

    await user.click(row);
    expect(within(panel).getByText(/只显示这一个运行/)).toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "显示全部" }));
    expect(within(panel).queryByText(/只显示这一个运行/)).not.toBeInTheDocument();
  });

  it("没委派过的会话里不画这块面板", async () => {
    // 与 Work 页同一条规则：每个运行都是这一回合本身时，面板只是家具。
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [delegated[0]!],
      thinking: "",
      thinkingCallId: "",
      answer: "",
    });

    mounted();
    await screen.findByLabelText("要做的事");

    expect(
      screen.queryByRole("region", { name: "参与这次任务的 Agent" }),
    ).not.toBeInTheDocument();
  });
});

describe("CodePage", () => {
  it("shows what the model is thinking while the turn is still running", async () => {
    const user = userEvent.setup();
    // Never resolves: the block is for a turn in flight, so the turn has to
    // still be in flight when the reasoning arrives.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    // A call id alongside the text, because that pair is what the hook
    // actually produces: it records a thought only when the delta carries both
    // a `model_call_id` and a non-empty slice, so text with no id is
    // unreachable. The id is what the thought's step is synthesised from --
    // the transient delta beats its own durable `ModelStarted` by up to a whole
    // catch-up poll, so there is nothing else yet to hang it on.
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [],
      thinking: "先看 notes.md 里有什么。",
      thinkingCallId: "mc_1",
      answer: "",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "整理待办");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // No `正在思考…` heading any more: the thought is the row, sitting where
    // the action it is about to cause will appear. A label saying that thinking
    // is happening, above a block of thinking, was saying it twice.
    const live = await screen.findByText("先看 notes.md 里有什么。");
    expect(live.closest("li.aw-code-step")?.className).toContain("is-live");

    // 反向断言，而这一条是补回来的：上面那句话原本只有正向的一半，于是横幅在
    // `codeLiveStatus` 里悄悄回来时没有任何东西报警（ADR-064 的回归）。一条只
    // 说「新形状在」的测试，管不住「旧形状也还在」。
    expect(screen.queryByText("正在思考下一步")).toBeNull();
    expect(screen.queryByText("分析目标并选择接下来的动作")).toBeNull();
    // 也不能落到那句更空的通用旁白上——它同样是压在一段具体的思考上面。
    expect(screen.queryByText("等待下一条执行记录")).toBeNull();
  });

  it("writes the report out as it arrives, not all at once at the end", async () => {
    const user = userEvent.setup();
    // Never resolves: the report is being written *now*, so the request has to
    // still be open when it is asserted.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [],
      thinking: "",
      thinkingCallId: "mc_1",
      answer: "已完成。新建了 clock.html，",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "写个时钟");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The regression this pins: `ModelDelta` reaches the browser -- it is
    // transient, and `ProcessOnlySink` fences only the three answer-publication
    // events -- and the hook used to drop it. Writing a report is the longest
    // stretch of a turn, and the console showed nothing moving for all of it.
    const streaming = await screen.findByText("已完成。新建了 clock.html，");
    expect(streaming.closest(".aw-code-report")?.className).toContain(
      "is-streaming",
    );
  });

  it("shows the script's own output as it is printed, and for how long", async () => {
    const user = userEvent.setup();
    // Never resolves: the call is running *now*, which is the only state this
    // block is ever drawn in.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map([
        [
          "call_1",
          {
            lines: [
              "executing in the sandbox",
              "processing chunk 0",
              "processing chunk 1",
              "stderr: warning: nothing to do",
            ],
            elapsedMs: 72_000,
            percent: null,
          },
        ],
      ]),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 1,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "sandbox_run",
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "跑一下");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The gap this closes: `sandbox_run` declares a 300-second timeout, and
    // between ToolStarted and ToolCompleted the console used to show the
    // tool's name and nothing else -- a script that had hung looked exactly
    // like one that was working (ADR-068). What is on screen now is the script
    // talking (ADR-069).
    expect(await screen.findByText("processing chunk 1")).toBeVisible();
    expect(screen.getByText("executing in the sandbox")).toBeVisible();
    // stderr is marked rather than merged: a traceback and ordinary output
    // read very differently and the reader has to be able to tell.
    expect(screen.getByText("stderr: warning: nothing to do")).toBeVisible();
    // Minutes and seconds, from `elapsed_ms` -- the clock is a number on the
    // wire and the words are this console's, so a CLI reading the same event
    // is not stuck with a Chinese sentence.
    expect(screen.getByText("已运行 1 分 12 秒")).toBeVisible();
  });

  it("re-reads the transcript for a run it cannot place", async () => {
    // The gap this closes, measured before it was closed: a turn posted to
    // this session from anywhere other than this tab's own request -- a reload
    // a second after sending, a second tab, a script -- arrived on the event
    // stream with no instruction to hang off. `buildTurnBlocks` refuses to
    // guess which turn owns it and drops it, so the pane said 这个会话还是空的
    // for the whole run while its steps streamed past underneath.
    //
    // The transcript is the fix: the server appends the user message *before*
    // the run starts, so the sentence is already on the server when the first
    // `RunStarted` reaches the browser. This tab just never asked again.
    vi.mocked(getCodeHistory)
      .mockResolvedValueOnce({ messages: [] })
      .mockResolvedValue({ messages: [{ role: "user", text: "跑一下" }] });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_elsewhere",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "RunStarted",
          sequence: 1,
          payload: { kind: "RunStarted" },
        },
        {
          event_id: "evt_2",
          run_id: "run_elsewhere",
          timestamp: "2026-08-14T12:00:01Z",
          event_type: "RunCompleted",
          sequence: 2,
          payload: { kind: "RunCompleted" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    // The instruction only exists in the *second* transcript, so seeing it at
    // all proves the page asked a second time on its own -- nothing in this
    // test sends anything, and `askCode` is never called.
    expect(await screen.findByText("跑一下")).toBeVisible();
    expect(vi.mocked(askCode)).not.toHaveBeenCalled();
  });

  it("shows a running call's progress on a turn this tab did not start", async () => {
    // The regression that shipped: progress was gated on `block.live`, which
    // `buildTurnBlocks` sets from whether *this tab's* ask request is open. A
    // reader who reloaded part way through a run, or opened a second tab, got
    // every step marked 进行中 with nothing under it -- while `ToolProgress`
    // frames arrived on the stream the whole time. That reader is precisely
    // the one asking "is this stuck?".
    //
    // `askCode` is never called here, so nothing this tab started is open and
    // `block.live` is false for every block. The steps still arrive, because
    // the event subscription is scoped to the session rather than to the turn.
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [{ role: "user", text: "跑一下" }],
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map([
        [
          "call_1",
          { lines: ["step 3", "step 4"], elapsedMs: 9000, percent: null },
        ],
      ]),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 1,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "sandbox_run",
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    expect(await screen.findByText("step 4")).toBeVisible();
    expect(screen.getByText("已运行 9 秒")).toBeVisible();
  });

  it("does not leave a moving line under a step that has stopped", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    // A map that still holds the call -- the belt to the hook's braces. The
    // hook drops an entry when the call returns; this asserts the renderer
    // would not draw it even if one survived, because the step's own outcome
    // is what gates it.
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map([
        [
          "call_1",
          { lines: ["processing chunk 0"], elapsedMs: 72_000, percent: null },
        ],
      ]),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 1,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "sandbox_run",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:01Z",
          event_type: "ToolCompleted",
          sequence: 2,
          payload: { kind: "ToolCompleted", tool_call_id: "call_1" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "跑一下");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("region", { name: "编码会话" });
    expect(screen.queryByText("processing chunk 0")).toBeNull();
    expect(screen.queryByText("已运行 1 分 12 秒")).toBeNull();
  });

  it("lets the server's report replace the streamed one without a gap", async () => {
    // The durable assistant message arrives on the transcript reload, which is
    // later than both `ModelCompleted` and `RunCompleted`. If the stream were
    // cleared on either, the report the reader just watched being written would
    // blank for as long as the reload takes.
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [
        { role: "user", text: "写个时钟" },
        { role: "assistant", text: "已完成。新建了 clock.html。" },
      ],
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [],
      thinking: "",
      thinkingCallId: "",
      // Still held by the hook, deliberately.
      answer: "已完成。新建了 clock.html，",
    });

    mounted();

    // Once the durable text exists it wins, and it is the only copy on screen.
    const report = await screen.findByText("已完成。新建了 clock.html。");
    expect(report.closest(".aw-code-report")?.className).not.toContain(
      "is-streaming",
    );
    expect(
      screen.queryByText("已完成。新建了 clock.html，"),
    ).not.toBeInTheDocument();
  });

  it("says nothing about thinking when no turn is running", () => {
    // The hook keeps its last value across a render; the block is gated on the
    // turn so a finished turn does not leave "正在思考…" over a written report.
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [],
      thinking: "leftover reasoning",
      thinkingCallId: "",
      answer: "",
    });

    mounted();

    expect(screen.queryByText("leftover reasoning")).not.toBeInTheDocument();
  });

  it("shows the report a turn came back with", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "Wrote notes.md.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });
    // The second read is what the page shows: the turn's own response is a
    // status, and the transcript comes from the server rather than from what
    // the client guessed it would say.
    vi.mocked(getCodeHistory)
      .mockResolvedValueOnce({ messages: [] })
      .mockResolvedValueOnce({
        messages: [
          { role: "user", text: "write notes.md" },
          { role: "assistant", text: "Wrote notes.md." },
        ],
      });

    mounted();
    await user.type(
      screen.getByLabelText("要做的事"),
      "write notes.md{Enter}",
    );

    await waitFor(() => {
      expect(screen.getByText("Wrote notes.md.")).toBeInTheDocument();
    });
    expect(vi.mocked(askCode).mock.calls[0]?.[2]).toBe("write notes.md");
  });

  it("offers three answers for a write, and two for an external tool", async () => {
    const user = userEvent.setup();
    // Never resolves: the approval poll only runs while a turn is in flight,
    // so the turn has to still be in flight when the question arrives.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getCodeApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_write",
          tool_name: "workspace_write",
          argument_digest: "a".repeat(64),
          approval_preview: '{"content":"# notes","path":"notes.md"}',
          risk: "write",
        },
        {
          approval_id: "apr_shell",
          tool_name: "sandbox_run",
          argument_digest: "b".repeat(64),
          approval_preview: '{"script":"print(1)"}',
          risk: "external",
        },
      ],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "run the tests");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const held = await screen.findByRole("region", { name: "待批准的调用" });
    const cards = within(held).getAllByRole("article");
    expect(within(cards[0] as HTMLElement).getAllByRole("button")).toHaveLength(
      3,
    );
    // A standing yes to an irreversible effect is refused by the server, so it
    // is not offered here either -- a button whose only outcome is a 422 is a
    // button that teaches the reader the wrong rule.
    expect(within(cards[1] as HTMLElement).getAllByRole("button")).toHaveLength(
      2,
    );
    expect(
      within(cards[1] as HTMLElement).queryByRole("button", {
        name: "本会话都允许",
      }),
    ).not.toBeInTheDocument();
  });

  it("shows what the call would do, not only its digest", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getCodeApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_run",
          tool_name: "project_run",
          argument_digest: "c".repeat(64),
          approval_preview: '{"command":"rm -rf build"}',
          risk: "destructive",
        },
      ],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "clean the build");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const held = await screen.findByRole("region", { name: "待批准的调用" });
    // The reason this test exists. Until the card carried a preview it showed
    // 64 hex characters and nothing else, which asked the same question of
    // `rm -rf build` and of `ls` -- and the person answering had no way to
    // tell which of the two they had just approved.
    expect(within(held).getByText(/rm -rf build/)).toBeInTheDocument();
    expect(within(held).getByText("不可撤销")).toBeInTheDocument();
  });

  it("sends the decision the button says, and stops showing the question", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(decideCodeApproval).mockResolvedValue(undefined);
    vi.mocked(getCodeApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_write",
          tool_name: "workspace_write",
          argument_digest: "a".repeat(64),
          approval_preview: '{"content":"# notes","path":"notes.md"}',
          risk: "write",
        },
      ],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write it");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const held = await screen.findByRole("region", { name: "待批准的调用" });
    await user.click(within(held).getByRole("button", { name: "拒绝" }));

    expect(vi.mocked(decideCodeApproval).mock.calls[0]?.slice(1)).toEqual([
      SESSION,
      "apr_write",
      "deny",
    ]);
  });

  it("says why a turn stopped when it produced no report", async () => {
    // A deadline-failed turn appends no assistant message at all -- the
    // server declines to invent one -- so without a notice the transcript
    // shows the instruction and then silence, which reads as a broken
    // session rather than a spent turn.
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "failed",
      stop_reason: "deadline",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "run the tests");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText(/这一轮到时间停下了/)).toBeInTheDocument();
    });
  });

  it("names the account when the provider refused it", async () => {
    // ADR-0084. Every provider failure arrives as `stop_reason: "error"`, so
    // before the code came with it this rendered `这一轮没有跑完（error）` --
    // and the reader, whose account had simply run out of credit, had nothing
    // to distinguish that from a retired model id.
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "",
      workspace_version: null,
      run_id: "run_1",
      status: "failed",
      stop_reason: "error",
      error_code: "provider_account_rejected",
      error_message:
        "the provider refused the request with HTTP 402: this deployment's " +
        "provider account is out of credit. Retrying will not help until " +
        "that is fixed.",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText(/余额用尽/)).toBeInTheDocument();
    });
    // The advice every other stopped-turn note ends on, withheld here for the
    // same reason `context_limit` withholds it: the next turn in this session
    // calls the same account and fails the same way.
    expect(screen.queryByText(/直接说下一步就能继续/)).not.toBeInTheDocument();
  });

  it("shows the server's own words for a failure it has no phrase for", async () => {
    // The fallback that replaced `（error）`. An unrecognised code is still
    // the most specific thing anyone has, and the message is more specific
    // than the code -- the same rule `explainFailure` applies to Task details.
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "",
      workspace_version: null,
      run_id: "run_1",
      status: "failed",
      stop_reason: "error",
      error_code: "provider_error",
      error_message: "the provider rejected the request with HTTP 503",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    });
  });

  it("says what went wrong instead of losing the turn", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockRejectedValue(new Error("这个会话已经在跑一轮了"));

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText("这个会话已经在跑一轮了")).toBeInTheDocument();
    });
  });

  it("asks nothing while no turn is running", async () => {
    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeHistory)).toHaveBeenCalled();
    });

    // The control for the poll's condition. A poll that ran regardless would
    // ask this once a second for as long as the tab is open.
    expect(vi.mocked(getCodeApprovals)).not.toHaveBeenCalled();
  });

  it("shows nothing wrong when nothing is wrong", async () => {
    // The control that a mocked module makes necessary. Every other test here
    // asserts something present; a call this page makes on mount and the mock
    // does not define would throw into the same catch that renders errors, and
    // all of them would still pass.
    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalled();
    });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the whole width until the reader asks to look at something", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "notes.md", size_bytes: 2048, media_type: "text/markdown" },
      ],
    });

    mounted();

    // Having files is no longer what mounts the right column. It used to be,
    // and the cost was permanent: from the first turn onward the column took
    // up to 560px whether or not anybody had asked to see anything.
    const entry = await screen.findByRole("button", { name: "工作区 1" });
    expect(
      screen.queryByRole("complementary", { name: "预览" }),
    ).not.toBeInTheDocument();

    await user.click(entry);

    const pane = await screen.findByRole("complementary", { name: "预览" });
    expect(within(pane).getByText("notes.md")).toBeInTheDocument();
    expect(within(pane).getByText("2.0 KB")).toBeInTheDocument();
  });

  it("re-reads the workspace after a turn, including one that failed", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockRejectedValue(new Error("这一轮失败了"));

    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalledTimes(1);
    });
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The pointer moves per write, so a turn that failed may still have left
    // files behind. Not re-reading is how the pane starts lying.
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalledTimes(2);
    });
  });

  it("shows one line per action, not one per protocol event", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    // One tool call, as the server actually emits it: five events for the call
    // plus the model turn that proposed it.
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_m1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelStarted",
          sequence: 1,
          payload: { kind: "ModelStarted", model_call_id: "mc_1" },
        },
        {
          event_id: "evt_m2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelCompleted",
          sequence: 2,
          payload: {
            kind: "ModelCompleted",
            model_call_id: "mc_1",
            text: "",
            tool_call_ids: ["call_1"],
          },
        },
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 3,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
            argument_preview: JSON.stringify({ name: "notes.md" }),
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "PermissionResolved",
          sequence: 4,
          payload: {
            kind: "PermissionResolved",
            tool_call_id: "call_1",
            effect: "allow",
          },
        },
        {
          event_id: "evt_3",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 5,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_4",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolCompleted",
          sequence: 6,
          payload: { kind: "ToolCompleted", tool_call_id: "call_1" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const turns = await screen.findByRole("region", { name: "编码会话" });
    // Six events, one action, inside one turn. Rendered one row per event this
    // read "模型调用已开始 / 模型调用已完成 / 工具调用已提出 / 权限检查已完成 /
    // 工具调用已开始 / 工具调用已完成" -- the log's vocabulary, leaving a reader
    // to work out that a file was written.
    //
    // Counted as the action rows themselves. `getAllByRole("listitem")` sees
    // every level at once -- the turn, the action, the produced-file card --
    // so it answers "how deep is the tree" rather than the question here,
    // which is how many actions the reader is offered.
    expect(turns.querySelectorAll(".aw-code-action")).toHaveLength(1);
    // No `第 N 轮` anywhere: that was a pseudo-stage invented so Work's
    // component had something to draw a node dot beside. The instruction is
    // already the heading of this block, and numbering it again named the same
    // thing twice.
    expect(within(turns).queryByText(/^第 \d+ 轮$/)).not.toBeInTheDocument();
    // And the action is visible without opening anything.
    expect(within(turns).getByText("写入工作区")).toBeVisible();
    // Once now, not twice. The digest existed to tell a reader what was inside
    // a closed fold; there is no fold, so the row speaks for itself.
    expect(within(turns).getAllByText("写入工作区")).toHaveLength(1);
    const diagnostic = within(turns).getByText("原始事件");
    const raw = turns.querySelector(".aw-code-raw pre");
    expect(raw).not.toBeVisible();
    await user.click(diagnostic);
    expect(raw).toBeVisible();
    expect(raw).toHaveTextContent("ToolCompleted");
    // The file it produced is a card in the conversation, not a row in a pane
    // on the far side of the screen.
    const card = within(turns).getByRole("button", { name: /notes\.md/ });
    expect(card).toBeInTheDocument();
  });

  it("shows a produced file as a card before the turn has finished", async () => {
    const user = userEvent.setup();
    // Never resolves: the claim is about *when* the card appears, and the turn
    // must still be in flight when it is asserted. There is no RunCompleted in
    // the stream below either -- nothing has told the page this turn is over.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "clock.html", size_bytes: 1174, media_type: "text/html" },
      ],
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 1,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolCompleted",
          sequence: 2,
          payload: {
            kind: "ToolCompleted",
            tool_call_id: "call_1",
            // The structured fact, published outside the observability gate
            // (ADR-063). The argument preview is deliberately absent here:
            // a 1174-byte page would carry its name, but a large one would
            // have it truncated away, and the card must not depend on size.
            workspace_writes: ["clock.html"],
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "写一个时钟页面");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // "生成的文件应该在对话生成中" is a claim about timing as much as place.
    // A card that only appeared once the report landed would be a file list
    // with better placement, not a record of what the instruction was making.
    const turns = await screen.findByRole("region", { name: "编码会话" });
    const outputs = await within(turns).findByRole("list", {
      name: "这一轮产出的文件",
    });
    expect(within(outputs).getByText("clock.html")).toBeInTheDocument();
    // And it says what the call did without claiming what the file is: this
    // stream never saw an earlier write, but the workspace has a history older
    // than the event window, so "新建" would be a guess.
    expect(
      within(outputs).getByText(/写入 · 1\.1 KB · HTML/),
    ).toBeInTheDocument();
  });

  it("keeps a finished turn's steps on screen", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "Wrote notes.md.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });
    // The server appends the user message before the run starts, so a settled
    // turn always reads back with its instruction. The block that holds the
    // steps hangs off that instruction, which is why this mock has to have it:
    // a run with no instruction to pair with is deliberately dropped rather
    // than guessed onto a neighbouring turn.
    vi.mocked(getCodeHistory)
      .mockResolvedValueOnce({ messages: [] })
      .mockResolvedValue({
        messages: [
          { role: "user", text: "write notes.md" },
          { role: "assistant", text: "Wrote notes.md." },
        ],
      });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 1,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:01Z",
          event_type: "RunCompleted",
          sequence: 2,
          payload: { kind: "RunCompleted" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The turn has settled -- the send button is back -- and the process it
    // went through is still readable. This pane used to be mounted only while
    // the request was in flight, over a list the hook emptied on the way out,
    // so the steps of the turn whose report is on screen were exactly the ones
    // that could never be looked at.
    // Waited on the transcript rather than on the send button: the composer
    // clears on submit, so the button stays disabled for an empty instruction
    // whether or not the turn is still running, and asserting on it would wait
    // for something that never becomes true.
    await waitFor(() => {
      expect(vi.mocked(getCodeHistory)).toHaveBeenCalledTimes(2);
    });
    // The steps of the turn whose report is on screen are still readable --
    // and now without a click, which is the half that changed. This pane used
    // to be mounted only while the request was in flight, over a list the hook
    // emptied on the way out.
    const turns = screen.getByRole("region", { name: "编码会话" });
    expect(turns.querySelectorAll(".aw-code-action")).toHaveLength(1);
    expect(within(turns).getByText("写入工作区")).toBeVisible();
  });

  it("lists sessions by the name their first instruction gave them", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: "2026-08-14T09:00:00Z",
          project_id: null,
        },
        {
          session_id: SESSION,
          title: null,
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    // From the server, so it survives a cleared browser and a different
    // machine. The row with no title is one opened and never spoken in.
    await user.click(
      // 正则而不是全等：这一行现在带着最后活动时间的副行（稿子上那条
      // 「… · 14:02」），控件的可及名字于是是「标题 + 时间」。名字里有
      // 时间是对的——它是这一行真正承载的两件事之一——所以收窄的是断言，
      // 不是标记。
      // `^` 不能省：同一行上的删除按钮叫「删除会话 把 notes.md …」，
      // 不锚定开头的话两个都匹配得上。
      await within(recent).findByRole("button", {
        name: /^把 notes\.md 整理成清单/,
      }),
    );

    await waitFor(() => {
      expect(vi.mocked(getCodeHistory).mock.calls.at(-1)?.[1]).toBe(
        "ses_code_older",
      );
    });
  });

  it("uploads from beside the composer, and the workspace pane appears with it", async () => {
    const user = userEvent.setup();
    vi.mocked(putCodeWorkspaceFile).mockResolvedValue({
      files: [{ name: "notes.txt", size_bytes: 11, media_type: "text/plain" }],
    });

    mounted();
    // Before anything is uploaded this session has no files, so there is no
    // workspace pane at all -- the conversation keeps the whole width.
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalled();
    });
    expect(
      screen.queryByRole("button", { name: /^工作区 / }),
    ).not.toBeInTheDocument();

    // The control sits with the composer: attaching a file is part of asking,
    // not a property of the file pane it used to live in.
    const file = new File(["hello world"], "notes.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText(/上传/), file);

    await waitFor(() => {
      expect(vi.mocked(putCodeWorkspaceFile).mock.calls[0]?.[2]).toBe(file);
    });
    // The listing the write answered with, not a refetch: the endpoint returns
    // it precisely so the pane does not have to ask again for something it was
    // just told.
    //
    // An uploaded file is exactly the case the produced-file cards cannot
    // cover -- no tool call wrote it, so no event names it -- which is why the
    // panel's fold is titled with the full count rather than "其他文件".
    await user.click(await screen.findByRole("button", { name: "工作区 1" }));
    const pane = await screen.findByRole("complementary", { name: "预览" });
    expect(await within(pane).findByText("notes.txt")).toBeInTheDocument();
    expect(within(pane).getByText(/工作区全部文件（1）/)).toBeInTheDocument();
  });

  it("opens on a centered start when there is no session", async () => {
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
          // 归在刚选的那个文件夹下：起始屏的列表按文件夹收窄，而这条用例问的
          // 是「这一栏在起始屏上展开着吗」，不是收窄本身——收窄有它自己的用例。
          project_id: PROJECT.project_id,
        },
      ],
    });

    const user = userEvent.setup();
    mounted("/code");
    await chooseFolder(user);

    expect(
      await screen.findByRole("heading", { name: "开始编码" }),
    ).toBeInTheDocument();
    // The recent list is unfolded here -- "where was I" is the likeliest
    // question a person with no session open is asking.
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    expect(
      await within(recent).findByRole("button", {
        name: "把 notes.md 整理成清单",
      }),
    ).toBeInTheDocument();
    // No panes about a session that does not exist.
    expect(
      screen.queryByRole("complementary", { name: "预览" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "编码会话" }),
    ).not.toBeInTheDocument();
    // Upload only appears after the first sentence creates a workspace. A
    // disabled paperclip on the start page was a control with no useful action.
    expect(screen.queryByLabelText(/上传/)).not.toBeInTheDocument();
  });

  it("opens the session on the first sentence and runs the turn in it", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockResolvedValue({
      session_id: SESSION,
      title: null,
    });
    vi.mocked(askCode).mockResolvedValue({
      report: "Done.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted("/code");
    await chooseFolder(user);
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The first sentence carries the POST the old splash screen demanded a
    // separate click for, and the turn runs against the session it opened.
    await waitFor(() => {
      expect(vi.mocked(askCode).mock.calls[0]?.[1]).toBe(SESSION);
    });
    expect(vi.mocked(createCodeSession)).toHaveBeenCalledTimes(1);
  });

  it("shows visible progress while the first session is being created", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockReturnValue(new Promise(() => undefined));

    mounted("/code");
    await chooseFolder(user);
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const pending = screen.getByRole("button", { name: "正在处理" });
    expect(pending.querySelector(".aw-spin")).toBeInTheDocument();
  });

  it("keeps the sentence on screen for the whole of the turn that opened the session", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockResolvedValue({
      session_id: SESSION,
      title: null,
    });
    // Held open, the way a real coding turn is: minutes, not a microtask.
    vi.mocked(askCode).mockReturnValue(new Promise(() => undefined));
    // What the route change re-reads. The server appends the user message only
    // when the turn starts, so this reload -- fired by the navigation, before
    // `askCode` is even sent -- sees nothing. It used to clear the pending
    // sentence anyway, and the pane then said 这个会话还是空的 under a spinner
    // for the entire turn: no instruction, no steps, no thinking, no report.
    vi.mocked(getCodeHistory).mockResolvedValue({ messages: [] });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      steps: [],
      thinking: "先看看工作区里有什么",
      thinkingCallId: "mc_1",
      answer: "",
    });

    mounted("/code");
    await chooseFolder(user);
    await user.type(screen.getByLabelText("要做的事"), "写一个 sq.py");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const transcript = await screen.findByRole("region", { name: "编码会话" });
    expect(
      await within(transcript).findByText("写一个 sq.py"),
    ).toBeInTheDocument();
    // And the block is the live one, so the thought in flight has somewhere to
    // land. This is the whole point of keeping the sentence: a block is what a
    // turn's steps, reasoning and report are drawn inside.
    expect(
      within(transcript).getByText("先看看工作区里有什么"),
    ).toBeInTheDocument();
    expect(screen.queryByText("这个会话还是空的")).not.toBeInTheDocument();
  });

  it("drops the pending copy once the server's transcript carries it", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockResolvedValue({
      session_id: SESSION,
      title: null,
    });
    vi.mocked(askCode).mockReturnValue(new Promise(() => undefined));
    // The other side of the race: this reload lands after the turn appended
    // the instruction. Two copies of one sentence is what the pending block
    // exists to avoid, so the server's copy has to be the one that survives.
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [{ role: "user", text: "写一个 sq.py" }],
    });

    mounted("/code");
    await chooseFolder(user);
    await user.type(screen.getByLabelText("要做的事"), "写一个 sq.py");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const transcript = await screen.findByRole("region", { name: "编码会话" });
    await waitFor(() => {
      expect(within(transcript).getAllByText("写一个 sq.py")).toHaveLength(1);
    });
  });

  it("puts a new session in the list before the turn it opened comes back", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockResolvedValue({
      session_id: SESSION,
      title: null,
    });
    vi.mocked(askCode).mockReturnValue(new Promise(() => undefined));

    mounted("/code");
    await chooseFolder(user);
    await user.type(
      screen.getByLabelText("要做的事"),
      "写一个 sq.py\n再加上注释",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));

    // Named from the first line of the instruction, the way the server names
    // it (ADR-047). The invalidation at the end of the turn replaces this with
    // the server's own copy; what this buys is the minutes in between, during
    // which the session being watched used to be the one session missing from
    // the list beside it.
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    // `^`，因为这一行现在有两个按钮叫得出这个名字：打开它的那个，和它旁边
    // 「重命名会话 写一个 sq.py」。而名字本身不再是精确匹配——正在跑的那一行
    // 把「（正在运行）」读给屏幕阅读器（见 `CodeSessionRail`），而这一轮正在
    // 跑，所以这里顺带也钉住了那个标记确实出现。
    const row = await within(recent).findByRole("button", {
      name: /^写一个 sq\.py/,
    });
    expect(row).toBeInTheDocument();
    expect(row.textContent).toContain("（正在运行）");
  });

  it("deletes a session only after the reader confirms", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: "2026-08-14T09:00:00Z",
          project_id: null,
        },
      ],
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    const remove = await within(recent).findByRole("button", {
      name: "删除会话 把 notes.md 整理成清单",
    });

    // Declined first. A delete that ran anyway would be irreversible, so the
    // refusal is the half worth asserting before the success.
    await user.click(remove);
    expect(vi.mocked(deleteCodeSession)).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(remove);
    await waitFor(() => {
      expect(vi.mocked(deleteCodeSession).mock.calls[0]?.[1]).toBe(
        "ses_code_older",
      );
    });
  });

  it("sends the reader to the start page when the session they are viewing is deleted", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "删除会话 第一句指令",
      }),
    );

    // The control for the case below: a delete that lands while its own
    // session is on screen still has to move the reader off it.
    // 起始页现在是那道门本身（ADR-074）：删掉最后一段会话之后回到的地方，
    // 是「在哪个文件夹里编码」，不是一个已经可以打字的输入框。
    expect(
      await screen.findByRole("heading", { name: "在哪个文件夹里编码？" }),
    ).toBeInTheDocument();
  });

  it("leaves the reader in the session they opened while a delete was in flight", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    let settleDelete: ((deleted: { session_id: string }) => void) | undefined;
    vi.mocked(deleteCodeSession).mockReturnValue(
      new Promise((resolve) => {
        settleDelete = resolve;
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "删除会话 第一句指令",
      }),
    );
    await waitFor(() => expect(deleteCodeSession).toHaveBeenCalledTimes(1));

    // A DELETE and the list refresh behind it are two round trips, and the
    // rail stays clickable for both.
    await user.click(
      within(recent).getByRole("button", { name: "把 notes.md 整理成清单" }),
    );
    await waitFor(() => {
      expect(
        within(recent).getByRole("button", {
          current: "page",
          name: "把 notes.md 整理成清单",
        }),
      ).toBeInTheDocument();
    });

    const finish = settleDelete;
    if (finish === undefined) throw new Error("Code delete mock did not start");
    await act(async () => {
      finish({ session_id: SESSION });
      await Promise.resolve();
    });
    // A frame, so that "did not navigate" is a fact rather than a head start:
    // the DELETE's continuation runs behind a list refresh.
    await nextFrame();

    // This is the regression. The callback closed over the session that was
    // open when the trash was clicked, so `target === sessionId` was still
    // true a second later and the reader was sent to /code for nothing.
    expect(
      within(recent).getByRole("button", {
        current: "page",
        name: "把 notes.md 整理成清单",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "开始编码" }),
    ).not.toBeInTheDocument();
  });

  it("does not report a delete that failed under one principal on the next one's page", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    let failDelete: ((cause: Error) => void) | undefined;
    vi.mocked(deleteCodeSession).mockReturnValue(
      new Promise((_resolve, reject) => {
        failDelete = reject;
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    // Rendered by hand rather than through `mounted()`: the identity has to
    // change *without* remounting the page, which is what the identity editor
    // actually does to a console left open on a session.
    const queries = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const tree = () => (
      <QueryClientProvider client={queries}>
        <MemoryRouter initialEntries={[`/code/${SESSION}`]}>
          <Routes>
            <Route element={<CodePage />} path="/code/:sessionId?" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(tree());

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "删除会话 第一句指令",
      }),
    );
    await waitFor(() => expect(deleteCodeSession).toHaveBeenCalledTimes(1));

    vi.mocked(useIdentity).mockReturnValue({
      identity: BOB,
      setIdentity: vi.fn(),
      editorOpen: false,
      setEditorOpen: vi.fn(),
    } as unknown as ReturnType<typeof useIdentity>);
    view.rerender(tree());

    const fail = failDelete;
    if (fail === undefined) throw new Error("Code delete mock did not start");
    await act(async () => {
      fail(new Error("这个会话删不掉"));
      await Promise.resolve();
    });
    await nextFrame();

    // Alice's refusal is not Bob's news, and the page he is looking at is not
    // where it could be acted on.
    expect(screen.queryByText("这个会话删不掉")).not.toBeInTheDocument();
  });

  it("does not blank the session the reader opened while a turn was still running", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    vi.mocked(getCodeHistory).mockImplementation((_identity, id) =>
      Promise.resolve({
        messages:
          id === "ses_code_older"
            ? [{ role: "user" as const, text: "另一个会话里说过的话" }]
            : [{ role: "user" as const, text: "这个会话里说过的话" }],
      }),
    );
    let settleAsk:
      ((answer: Awaited<ReturnType<typeof askCode>>) => void) | undefined;
    vi.mocked(askCode).mockReturnValue(
      new Promise((resolve) => {
        settleAsk = resolve;
      }),
    );

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "跑一下");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(askCode).toHaveBeenCalledTimes(1));

    // A coding turn holds its request open for minutes, and the rail stays
    // clickable for all of it -- which is what the rail is for.
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "把 notes.md 整理成清单",
      }),
    );
    expect(await screen.findByText("另一个会话里说过的话")).toBeVisible();

    const finish = settleAsk;
    if (finish === undefined) throw new Error("Code ask mock did not start");
    await act(async () => {
      finish({
        report: "写完了。",
        workspace_version: "art_1",
        run_id: "run_1",
        status: "completed",
        stop_reason: "completed",
      });
      await Promise.resolve();
    });
    await nextFrame();

    // This is the regression. The reload at the end of a turn wrote
    // `loadedFor` unconditionally, and `messages` is derived as
    // `loadedFor === sessionId ? loadedMessages : []` -- so landing the first
    // session's id while the route said the second collapsed the pane to
    // 这个会话还是空的 over a session with a full history, and nothing on the
    // page ever put it back.
    expect(screen.getByText("另一个会话里说过的话")).toBeVisible();
    expect(screen.queryByText("这个会话还是空的")).not.toBeInTheDocument();
  });

  it("leaves the other session usable while a turn runs in this one", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    vi.mocked(askCode).mockReturnValue(new Promise(() => undefined));

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "跑一下");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(askCode).toHaveBeenCalledTimes(1));
    // The session it was typed into is busy, and says so.
    expect(screen.getByRole("button", { name: "正在处理" })).toBeDisabled();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      within(recent).getByRole("button", { name: "把 notes.md 整理成清单" }),
    );

    // The other one is not. A page-wide `running` flag disabled this composer
    // and hung the first session's spinner on it, so a reader who switched
    // sessions mid-turn could not send anything anywhere.
    const send = await screen.findByRole("button", { name: "发送" });
    await user.type(screen.getByLabelText("要做的事"), "另一件事");
    expect(send).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "正在处理" }),
    ).not.toBeInTheDocument();
  });

  it("marks the session being viewed, even when it has no name yet", async () => {
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: null,
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    expect(
      await within(recent).findByRole("button", { current: "page" }),
    ).toBeInTheDocument();
  });

  it("returns focus to the Code rename action when Escape cancels editing", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "重命名会话 第一句指令",
      }),
    );
    await user.type(
      within(recent).getByLabelText("会话名字"),
      "改了一半{Escape}",
    );

    expect(within(recent).queryByLabelText("会话名字")).not.toBeInTheDocument();
    expect(
      within(recent).getByRole("button", { name: "第一句指令" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        within(recent).getByRole("button", { name: "重命名会话 第一句指令" }),
      ).toHaveFocus();
    });
    expect(vi.mocked(renameCodeSession)).not.toHaveBeenCalled();
  });

  it("offers an explicit keyboard-accessible rename action", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "重命名会话 第一句指令",
      }),
    );
    expect(within(recent).getByLabelText("会话名字")).toBeInTheDocument();
  });

  it("renames a session to what a person called it", async () => {
    const user = userEvent.setup();
    // The server's answer changes after the rename, so the sidebar can only
    // show the new name by asking again.
    vi.mocked(listCodeSessions)
      .mockResolvedValueOnce({
        sessions: [
          {
            session_id: SESSION,
            title: "第一句指令",
            last_activity_at: null,
            project_id: null,
          },
        ],
      })
      .mockResolvedValue({
        sessions: [
          {
            session_id: SESSION,
            title: "重构工作区",
            last_activity_at: null,
            project_id: null,
          },
        ],
      });

    mounted();

    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "重命名会话 第一句指令",
      }),
    );
    const field = within(recent).getByLabelText("会话名字");
    await user.clear(field);
    await user.type(field, "重构工作区{Enter}");

    // The first instruction is a decent name and a chosen one is better; this
    // is the only path that overwrites what the instruction set.
    await waitFor(() => {
      expect(vi.mocked(renameCodeSession).mock.calls[0]?.slice(1)).toEqual([
        SESSION,
        "重构工作区",
      ]);
    });
    // And the sidebar shows it. A rename that did not refetch would leave the
    // old name sitting there until something unrelated happened to refresh.
    expect(
      await within(recent).findByRole("button", { name: "重构工作区" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        within(recent).getByRole("button", { name: "重命名会话 重构工作区" }),
      ).toHaveFocus();
    });
  });

  it("keeps a failed Code rename inline, focused, and retryable", async () => {
    const user = userEvent.setup();
    vi.mocked(renameCodeSession).mockRejectedValueOnce(
      new Error("名字没有保存"),
    );
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "重命名会话 第一句指令",
      }),
    );
    const field = within(recent).getByLabelText("会话名字");
    await user.clear(field);
    await user.type(field, "第一次{Enter}");

    expect(await within(recent).findByRole("alert")).toHaveTextContent(
      "名字没有保存",
    );
    expect(field).toHaveFocus();
    expect(field).not.toHaveAttribute("readonly");

    await user.clear(field);
    await user.type(field, "第二次{Escape}");
    await waitFor(() => {
      expect(
        within(recent).getByRole("button", { name: "重命名会话 第一句指令" }),
      ).toHaveFocus();
    });
  });

  it("does not pull focus back to a Code row the reader left mid-rename", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "第一句指令",
          last_activity_at: null,
          project_id: null,
        },
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });
    let settleRename:
      | ((accepted: {
          session_id: string;
          title: string | null;
          last_activity_at: string | null;
          project_id: string | null;
        }) => void)
      | undefined;
    vi.mocked(renameCodeSession).mockReturnValue(
      new Promise((resolve) => {
        settleRename = resolve;
      }),
    );

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.click(
      await within(recent).findByRole("button", {
        name: "重命名会话 第一句指令",
      }),
    );
    const field = within(recent).getByLabelText("会话名字");
    await user.clear(field);
    await user.type(field, "重构工作区{Enter}");
    await waitFor(() => expect(renameCodeSession).toHaveBeenCalledTimes(1));

    // `onBlur` declines to cancel while a rename is pending -- deliberately,
    // so the request keeps its row -- which is exactly why the round trip can
    // end somewhere the reader no longer is.
    const other = within(recent).getByRole("button", {
      name: "把 notes.md 整理成清单",
    });
    await user.click(other);

    const finish = settleRename;
    if (finish === undefined) throw new Error("Code rename mock did not start");
    await act(async () => {
      finish({
        session_id: SESSION,
        title: "重构工作区",
        last_activity_at: null,
        project_id: null,
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        within(recent).queryByLabelText("会话名字"),
      ).not.toBeInTheDocument();
    });
    await nextFrame();
    expect(other).toHaveFocus();
  });

  it("uses double-click only to open another Code session, not to rename it", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "当前会话",
          last_activity_at: null,
          project_id: null,
        },
        {
          session_id: "ses_code_2",
          title: "另一个会话",
          last_activity_at: null,
          project_id: null,
        },
      ],
    });

    mounted();
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    await user.dblClick(
      await within(recent).findByRole("button", { name: "另一个会话" }),
    );

    expect(
      await screen.findByRole("heading", { name: "另一个会话" }),
    ).toBeInTheDocument();
    expect(within(recent).queryByLabelText("会话名字")).not.toBeInTheDocument();
    expect(renameCodeSession).not.toHaveBeenCalled();
  });

  it("routes a produced code file to the panel instead of unfolding it in place", async () => {
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "collatz.py", size_bytes: 554, media_type: "text/plain" },
      ],
    });
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [
        { role: "user", text: "新建 collatz.py" },
        { role: "assistant", text: "写好了。" },
      ],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "def collatz_steps(n):\n    return 0",
      truncated: false,
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 1,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolCompleted",
          sequence: 2,
          payload: {
            kind: "ToolCompleted",
            tool_call_id: "call_1",
            workspace_writes: ["collatz.py"],
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    // 卡片底下曾经有一个「就地预览」折叠，小文件还会自己展开。它没了：
    // 所有预览统一走右侧那块面，卡片是去那里的入口。
    //
    // 这条钉住的是删除本身的两半——不擅自取（下面这句断言），以及点一下
    // 仍然能看到（再下面那句）。只钉前半句的话，一个把卡片也做哑了的改动
    // 会照样通过。
    const user = userEvent.setup();
    const turns = await screen.findByRole("region", { name: "编码会话" });
    const card = await within(turns).findByRole("button", {
      name: /collatz\.py/,
    });
    expect(within(turns).queryByText(/def collatz_steps/)).toBeNull();
    expect(vi.mocked(getCodeWorkspaceFileText)).not.toHaveBeenCalled();

    await user.click(card);
    expect(
      await within(
        await screen.findByRole("complementary", { name: "预览" }),
      ).findByText(/def collatz_steps/),
    ).toBeInTheDocument();
  });

  it("does not fetch a large produced file nobody asked to see", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      // 大文件。此前它证明的是「超过自动预览上限就不取」；折叠没了之后
      // 任何大小都不会被擅自取，所以这条改成证明「点开仍然给得出头部，
      // 并且照实说被截断了」——那才是这个尺寸还剩下的独有行为。
      files: [
        { name: "dump.txt", size_bytes: 900_000, media_type: "text/plain" },
      ],
    });
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [{ role: "user", text: "导出全部" }],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "head of it",
      truncated: true,
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 1,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolCompleted",
          sequence: 2,
          payload: {
            kind: "ToolCompleted",
            tool_call_id: "call_1",
            workspace_writes: ["dump.txt"],
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    const turns = await screen.findByRole("region", { name: "编码会话" });
    // Scoped to the card, because the name is now on screen twice on purpose:
    // the step row above it says 写入工作区 · dump.txt, which is what makes a
    // write say what it wrote. A bare getByText matches both and throws.
    expect(
      within(turns).getByText("dump.txt", {
        selector: ".aw-code-output-name",
      }),
    ).toBeInTheDocument();
    expect(vi.mocked(getCodeWorkspaceFileText)).not.toHaveBeenCalled();

    await user.click(within(turns).getByRole("button", { name: /dump\.txt/ }));
    const panel = await screen.findByRole("complementary", { name: "预览" });
    expect(await within(panel).findByText("head of it")).toBeInTheDocument();
    expect(
      within(panel).getByText("只显示了开头一部分，完整内容请下载。"),
    ).toBeInTheDocument();
  });

  it("keeps sessions and files in separate landmarks", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "notes.md", size_bytes: 12, media_type: "text/markdown" },
      ],
    });
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "上一个会话",
          last_activity_at: "2026-08-14T09:00:00Z",
          project_id: null,
        },
      ],
    });

    mounted();

    // The two lists used to sit in one column, and nested that way the session
    // rows were rows of the region labelled 工作区文件 -- so anything reading
    // that region by its label, a screen reader first among them, read out
    // session ids as files. They are now two landmarks on opposite sides.
    const rail = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    expect(within(rail).getByText("上一个会话")).toBeInTheDocument();
    expect(within(rail).queryByText("notes.md")).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "工作区 1" }));
    const pane = await screen.findByRole("complementary", { name: "预览" });
    expect(within(pane).getByText("notes.md")).toBeInTheDocument();
    expect(within(pane).queryByText("上一个会话")).not.toBeInTheDocument();
  });

  it("shows the reasoning of one model call exactly once", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      // The call still reasoning. `useCodeStream` clears this the moment that
      // call's ModelCompleted arrives, which is the other half of the
      // invariant asserted below.
      thinking: "现在该把文件读回来核对一遍",
      // The second call, the one still streaming. It has a ModelStarted and no
      // ModelCompleted, so it is the anchor the live text lands on.
      thinkingCallId: "mc_2",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelCompleted",
          sequence: 1,
          payload: {
            kind: "ModelCompleted",
            model_call_id: "mc_1",
            text: "",
            tool_call_ids: ["call_1"],
            thinking_preview: "先建一个空的 clock.html",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 2,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_3",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelStarted",
          sequence: 3,
          payload: { kind: "ModelStarted", model_call_id: "mc_2" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "写一个时钟页面");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const turns = await screen.findByRole("region", { name: "编码会话" });
    // The guard for the complaint that started this. Before it, one call's
    // excerpt could be on screen three times at once: streaming at the top of
    // the page, formatted as a 思考摘要 body inside its step, and verbatim
    // again inside that step's raw JSON payload dump.
    //
    // Once each, and never the same call twice. The invariant is unchanged;
    // what changed is how it holds. It used to be two disjoint sets rendered in
    // two places; it is now one row per call that settles in place -- a call
    // with a live thought has no ModelCompleted yet, so it cannot also be
    // supplying a durable excerpt.
    expect(within(turns).getAllByText("先建一个空的 clock.html")).toHaveLength(
      1,
    );
    expect(
      within(turns).getAllByText("现在该把文件读回来核对一遍"),
    ).toHaveLength(1);
    // Neither disclosure exists any more: a reader sees the commands and the
    // reasoning without clicking anything.
    expect(within(turns).queryByText("想过什么")).not.toBeInTheDocument();
    expect(within(turns).queryByText("做了什么")).not.toBeInTheDocument();
    // The settled thought sits on the action it caused -- one `<li>`, thought
    // above command. This is the whole point of the change.
    const settled = within(turns)
      .getByText("先建一个空的 clock.html")
      .closest("li.aw-code-step");
    expect(settled).not.toBeNull();
    expect(
      within(settled as HTMLElement).getByText("写入工作区"),
    ).toBeInTheDocument();
    // The live one is on its own step, marked, with no action under it yet.
    const streaming = within(turns)
      .getByText("现在该把文件读回来核对一遍")
      .closest("li.aw-code-step");
    expect(streaming?.className).toContain("is-live");
    expect(
      within(streaming as HTMLElement).queryByText("写入工作区"),
    ).not.toBeInTheDocument();
  });

  it("shows what a finished turn did without a click", async () => {
    // The direct regression test for the complaint that started this work: a
    // settled turn used to render two closed disclosures, so a reader saw no
    // command and no reasoning until they clicked twice.
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [
        { role: "user", text: "写个时钟" },
        { role: "assistant", text: "写好了。" },
      ],
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelCompleted",
          sequence: 1,
          payload: {
            kind: "ModelCompleted",
            model_call_id: "mc_1",
            text: "",
            tool_call_ids: ["call_1"],
            thinking_preview: "先建一个空的 clock.html",
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 2,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_3",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:01Z",
          event_type: "RunCompleted",
          sequence: 3,
          payload: { kind: "RunCompleted" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    const turns = await screen.findByRole("region", { name: "编码会话" });
    // No interaction at all before these two.
    expect(within(turns).getByText("写入工作区")).toBeInTheDocument();
    expect(
      within(turns).getByText("先建一个空的 clock.html"),
    ).toBeInTheDocument();
  });

  it("keeps a long thought's conclusion one click away rather than cutting it", async () => {
    const user = userEvent.setup();
    const head = "先看看工作区里有什么。";
    const rest = "然后决定是新建还是改已有的那个文件，再把结果读回来核对。";
    vi.mocked(getCodeHistory).mockResolvedValue({
      messages: [{ role: "user", text: "写个时钟" }],
    });
    vi.mocked(useCodeStream).mockReturnValue({
      progress: new Map(),
      thinking: "",
      thinkingCallId: "",
      answer: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelCompleted",
          sequence: 1,
          payload: {
            kind: "ModelCompleted",
            model_call_id: "mc_1",
            text: "",
            tool_call_ids: ["call_1"],
            thinking_preview: `${head}${rest}`,
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 2,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_list",
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();

    const turns = await screen.findByRole("region", { name: "编码会话" });
    // Folded to its first sentence, not truncated: truncation throws away the
    // half the reader came for, folding puts it behind one click.
    // 首句现在包在一个 span 里，因为它要能被 CSS 钳到两行——`THOUGHT_HEAD_MAX`
    // 管字符数，钳位管行数，而读者扫的是行。所以从文本往上找那个 `<summary>`。
    const summary = within(turns).getByText(head).closest("summary");
    if (summary === null) throw new Error("首句不在一个 <summary> 里");
    const fold = summary.closest("details");
    expect((fold as HTMLDetailsElement).open).toBe(false);

    await user.click(summary);
    expect((fold as HTMLDetailsElement).open).toBe(true);
    expect(within(turns).getByText(rest)).toBeInTheDocument();
  });

  it("shows what a text file holds, without spending a turn to ask", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "notes.md", size_bytes: 12, media_type: "text/markdown" },
      ],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "- ship it",
      truncated: false,
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /notes\.md/ }));

    // Before this the only way to see a file was to ask the agent to read it
    // back -- a model call to answer a question the store already knows.
    //
    // A `.md` arrives as the document it was written to be (F-28): `- ship it`
    // is a list item, so the text node is the item and the dash is the markup
    // that produced it. Asserting the `<li>` rather than the string is the
    // point -- a `<pre>` holding the source would satisfy `findByText("ship
    // it")` too if the query were loose enough.
    const item = await screen.findByText("ship it");
    expect(item.closest("li")).not.toBeNull();
    expect(vi.mocked(getCodeWorkspaceFileText).mock.calls[0]?.slice(1)).toEqual(
      [SESSION, "notes.md"],
    );

    // 源码 gives the bytes back, and gives them back **without a second
    // fetch**: both views are served from one cache entry, which is why the
    // markdown arm reuses the plain arm's `load` and key.
    await user.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.getByText("- ship it")).toBeInTheDocument();
    expect(vi.mocked(getCodeWorkspaceFileText).mock.calls).toHaveLength(1);
  });

  it("runs a .py from the panel, and never shows one file's output under another's name", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "maker.py", size_bytes: 60, media_type: "text/x-python" },
        { name: "broken.py", size_bytes: 40, media_type: "text/x-python" },
      ],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "print('hi')",
      truncated: false,
    });
    vi.mocked(runCodeWorkspaceFile).mockResolvedValue({
      exit_code: 0,
      stdout: "wrote report.txt\n",
      stderr: "",
      written: ["report.txt"],
      workspace_version: "art_2",
      omitted_inputs: [],
    });

    mounted();
    await openWorkspace(user, 2);
    await user.click(await screen.findByRole("button", { name: /maker\.py/ }));
    await user.click(await screen.findByRole("button", { name: "运行结果" }));

    expect(await screen.findByText(/运行结束，退出码 0/)).toBeInTheDocument();
    expect(vi.mocked(runCodeWorkspaceFile).mock.calls[0]?.slice(1)).toEqual([
      SESSION,
      "maker.py",
    ]);
    // Files a run produced land in the listing the page owns, so the count in
    // the header and the directory below it stop being one short.
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace).mock.calls.length).toBeGreaterThan(1);
    });

    // The panel keeps one tree position and swaps the file under it, so the
    // viewer is reused unless it is keyed -- and a `useMutation`'s result
    // outlives a prop change. Observed on a real session before the key:
    // `maker.py`'s stdout, under the heading `broken.py`.
    await user.click(await screen.findByRole("button", { name: /broken\.py/ }));
    expect(screen.queryByText(/运行结束，退出码 0/)).not.toBeInTheDocument();
    expect(vi.mocked(runCodeWorkspaceFile)).toHaveBeenCalledTimes(1);
  });

  it("names the picture a run drew, and routes it to the panel", async () => {
    const user = userEvent.setup();
    // ADR-066 写的那个场景：一个画图的脚本。在它之前，运行只用灰字答一句
    // 「写回工作区：plot.png」，图要两次点击才看得到——展开折叠的工作区列表，
    // 再找那个名字。
    //
    // ADR-066 的解法是让图自己展开；「就地预览」折叠删掉之后解法换成一次
    // 点击，但这条测试要守的东西没变：运行写出的东西要以卡片出现在它自己的
    // 列表里，而不是退化成一行灰字。
    vi.mocked(getCodeWorkspace)
      .mockResolvedValueOnce({
        files: [
          { name: "plot.py", size_bytes: 200, media_type: "text/x-python" },
        ],
      })
      .mockResolvedValue({
        files: [
          { name: "plot.py", size_bytes: 200, media_type: "text/x-python" },
          { name: "plot.png", size_bytes: 9000, media_type: "image/png" },
        ],
      });
    vi.mocked(runCodeWorkspaceFile).mockResolvedValue({
      exit_code: 0,
      stdout: "已生成 plot.png\n",
      stderr: "",
      written: ["plot.png"],
      workspace_version: "art_2",
      omitted_inputs: [],
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /plot\.py/ }));
    await user.click(await screen.findByRole("button", { name: "运行结果" }));

    // Scoped to the run's own list. The name is also in the workspace
    // directory below, and an unscoped query would pass on that one -- which
    // is precisely the two-click route this test exists to say is no longer
    // the only one.
    const produced = await screen.findByRole("list", {
      name: "这次运行写出的文件",
    });
    expect(
      within(produced).getByRole("button", { name: /plot\.png/ }),
    ).toBeInTheDocument();
    // 不再擅自取字节：卡片先只画名字和大小，点了才去拿。
    expect(vi.mocked(getCodeWorkspaceFileBlob)).not.toHaveBeenCalled();

    // And the panel refuses to call a zero exit code a success. stdout says
    // 已生成; only the picture says whether the labels rendered.
    expect(screen.getByText(/没说明它写对了/)).toBeInTheDocument();
  });

  it("draws a run's output from the run's own listing, not the page's", async () => {
    const user = userEvent.setup();
    // The page's listing never learns about the file. Before the response
    // carried one, that was the ordinary state for one render after every run
    // -- the names had arrived and the entries had not -- and the produced
    // files degraded to a line of grey text until a refetch landed. The run
    // now answers with the working set it left behind, so the card is drawable
    // immediately (known-gaps F-15).
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "plot.py", size_bytes: 200, media_type: "text/x-python" },
      ],
    });
    vi.mocked(runCodeWorkspaceFile).mockResolvedValue({
      exit_code: 0,
      stdout: "",
      stderr: "",
      written: ["plot.png"],
      workspace_version: "art_2",
      omitted_inputs: [],
      files: [
        { name: "plot.py", size_bytes: 200, media_type: "text/x-python" },
        { name: "plot.png", size_bytes: 9000, media_type: "image/png" },
      ],
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /plot\.py/ }));
    await user.click(await screen.findByRole("button", { name: "运行结果" }));

    const produced = await screen.findByRole("list", {
      name: "这次运行写出的文件",
    });
    expect(
      within(produced).getByRole("button", { name: /plot\.png/ }),
    ).toBeInTheDocument();
  });

  it("names a run's output without drawing dead buttons when nothing lists it", async () => {
    const user = userEvent.setup();
    // A server older than the `files` field, and a page listing that has not
    // caught up. All-or-nothing: a card here would open nothing and report
    // 已不在工作区 about a file written a second ago.
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "plot.py", size_bytes: 200, media_type: "text/x-python" },
      ],
    });
    vi.mocked(runCodeWorkspaceFile).mockResolvedValue({
      exit_code: 0,
      stdout: "",
      stderr: "",
      written: ["plot.png"],
      workspace_version: "art_2",
      omitted_inputs: [],
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /plot\.py/ }));
    await user.click(await screen.findByRole("button", { name: "运行结果" }));

    expect(await screen.findByText(/写回工作区：plot.png/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /plot\.png/ }),
    ).not.toBeInTheDocument();
  });

  it("offers a download for a type it cannot show, and does not fetch it", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "report.docx", size_bytes: 4096, media_type: DOCX_MEDIA_TYPE },
      ],
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(
      await screen.findByRole("button", { name: /report\.docx/ }),
    );

    // A .docx is told where its viewer *is*, not that none exists: the layout
    // endpoints address an artifact id and a workspace file has none (F-11).
    // The generic sentence is reserved for types nothing here can show.
    expect(
      await screen.findByText(/Word 的版面预览目前只在任务产出里有/),
    ).toBeInTheDocument();
    // Not fetched at all: reading a zip as text would render mojibake, and
    // downloading the bytes only to decide not to show them wastes the transfer.
    expect(vi.mocked(getCodeWorkspaceFileText)).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "下载" }));
    expect(
      vi.mocked(downloadCodeWorkspaceFile).mock.calls[0]?.slice(1),
    ).toEqual([SESSION, "report.docx"]);
  });

  it("runs an html file in a sandboxed frame instead of showing its source", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "demo.html", size_bytes: 64, media_type: "text/html" }],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "<html><body><h1>demo</h1></body></html>",
      truncated: false,
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /demo\.html/ }));

    // Rendered, not read: the page an agent builds only answers "did it
    // work?" by running. The sandbox value is pinned in HtmlPreview's own
    // tests; here the claim is that the workspace routes html into it.
    const frame = await screen.findByTitle("demo.html 预览");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    // The source stays one toggle away rather than being the default view.
    expect(screen.getByRole("button", { name: "源码" })).toBeInTheDocument();
  });

  it("says a long file was cut rather than implying that is all of it", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "big.txt", size_bytes: 999_999, media_type: "text/plain" },
      ],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "head of it",
      truncated: true,
    });

    mounted();
    await openWorkspace(user, 1);
    await user.click(await screen.findByRole("button", { name: /big\.txt/ }));

    // A truncated preview that said nothing would read as a complete file, and
    // the reader would go looking for content that is in the file but not here.
    expect(
      await screen.findByText("只显示了开头一部分，完整内容请下载。"),
    ).toBeInTheDocument();
  });

  it("lists the sessions of the folder you are in, and says how many it left out", async () => {
    // 收窄之前，这一栏列的是这个人所有的编码会话，而屏幕上其余的一切——目录树、
    // 起始屏那句「在 … 里编码」、agent 实际读写的文件——说的都是一个文件夹。
    // 一栏里两种范围，读者要自己在每一行上判断「这条是不是这儿的」。
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "这个文件夹里的",
          last_activity_at: "2026-08-22T09:00:00Z",
          project_id: PROJECT.project_id,
        },
        {
          session_id: "ses_code_elsewhere",
          title: "另一个文件夹里的",
          last_activity_at: "2026-08-21T09:00:00Z",
          project_id: "prj_2",
        },
      ],
    });

    mounted();
    const rail = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    expect(await within(rail).findByText("这个文件夹里的")).toBeInTheDocument();
    expect(within(rail).queryByText("另一个文件夹里的")).not.toBeInTheDocument();

    // 数出来，而不是安静地少列几行：一个不说数量的「全部显示」是在让读者猜
    // 自己有没有漏掉东西。
    await user.click(
      within(rail).getByRole("button", { name: /另外 1 段在别的文件夹/ }),
    );
    expect(
      await within(rail).findByText("另一个文件夹里的"),
    ).toBeInTheDocument();

    // 到了「全部」这一档，每一行才说得出它属于哪个文件夹。
    //
    // 这是切换器存在的**理由**的另一半：读者按它就是为了看到别处的会话，而在
    // 这一行之前，来自不同文件夹的行长得一模一样——两段同名的会话（同一句话在
    // 两个文件夹里各说过一次，是很常见的事）在这一列里不可分辨。
    //
    // 名字来自一份**已经被取着**的项目列表（`["projects", identity]`，
    // `ProjectChooser` 和 `ProjectPicker` 都在订阅它），所以这是一次 join，不是
    // 每行一次查询——后者正是 ADR-047 §4 拒绝在这一行上放轮数和文件数的理由。
    const elsewhere = within(rail)
      .getByText("另一个文件夹里的")
      .closest("button");
    expect(elsewhere?.textContent).toContain("别的文件夹");
    const here = within(rail).getByText("这个文件夹里的").closest("button");
    expect(here?.textContent).toContain(PROJECT.name);

    // 收窄回去，标记就该消失：整份列表都在同一个文件夹里的时候，在每一行上重复
    // 那个名字，是把一个不区分任何东西的字段印 N 遍。
    await user.click(
      within(rail).getByRole("button", { name: /只看这个文件夹/ }),
    );
    await waitFor(() => {
      expect(
        within(rail).queryByText("另一个文件夹里的"),
      ).not.toBeInTheDocument();
    });
    expect(
      within(rail).getByText("这个文件夹里的").closest("button")?.textContent,
    ).not.toContain(PROJECT.name);
  });

  it("folds the preview column away and remembers that, instead of popping it over the conversation", async () => {
    // 它是一栏，不是浮层：所以「关闭」变成了「收起」，而收起是一次表态——
    // 此前每开一个会话都是关着的，一个想一边读一边看文件的人得每次重点一遍。
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "notes.md", size_bytes: 12, media_type: "text/markdown" },
      ],
    });

    const first = mounted();
    await openWorkspace(user, 1);
    expect(
      await screen.findByRole("complementary", { name: "预览" }),
    ).toBeInTheDocument();

    // 重新挂载一次就是「换一个会话 / 刷新一次页面」：展开状态要活过它。
    first.unmount();
    mounted();
    expect(
      await screen.findByRole("complementary", { name: "预览" }),
    ).toBeInTheDocument();

    // 同一颗按钮收起它。一个点开了就再也不管用的开关，读者会以为它坏了。
    await user.click(await screen.findByRole("button", { name: "工作区 1" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("complementary", { name: "预览" }),
      ).not.toBeInTheDocument();
    });
  });

  it("opens a project file in that same column, not in a second layer of its own", async () => {
    // 两个来源（会话产出、项目目录）此前各有各的浮层，宽度还不一样，而且能
    // 互相盖住——同一个动作在屏幕上有两种样子。
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "这个文件夹里的",
          last_activity_at: "2026-08-22T09:00:00Z",
          project_id: PROJECT.project_id,
        },
      ],
    });
    vi.mocked(listProjectFiles).mockResolvedValue({
      path: "",
      entries: [
        {
          path: "main.py",
          kind: "file",
          size_bytes: 20,
          modified_at: "2026-08-22T00:00:00Z",
        },
      ],
      truncated: false,
    });
    vi.mocked(readProjectFile).mockResolvedValue({
      path: "main.py",
      text: "print('hi')",
      size_bytes: 20,
      is_text: true,
      modified_at: "2026-08-22T00:00:00Z",
    });

    mounted();
    await user.click(await screen.findByRole("button", { name: /main\.py/ }));

    const pane = await screen.findByRole("complementary", { name: "预览" });
    expect(within(pane).getByText("print('hi')")).toBeInTheDocument();
    // 一栏，一个页眉。名字长在那一栏的头上，而不是这一块自己再画一个。
    expect(
      within(pane).getByRole("heading", { name: "main.py" }),
    ).toBeInTheDocument();
  });
});
