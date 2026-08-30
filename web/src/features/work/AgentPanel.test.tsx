/**
 * 副面板对读者说的话。
 *
 * `runTree.test.ts` 钉的是事件描述出的那棵树，这一份钉的是那棵树被读出来的样子
 * ——两者不是一回事，`RunPanel.test.tsx` 开头那段话说的就是这个：树builder 有
 * 十二个测试，画它的那块面一个都没有，于是树里算对的东西被画丢了也没人红。
 *
 * 这里特别盯三类容易被画丢的东西：
 *
 * 一是**没有分母的时候不要画分数**。`max_total_tokens` 在这个仓库出厂的每个档里
 * 都是 `None`，只有委派那一档才有值，所以「没有上限」是常见情况而不是边角。
 *
 * 二是**父亲不在这一页时孩子仍然是孩子**。事件窗口只留最近一段，孩子在、父亲不
 * 在是会发生的；那时 `buildRunTree` 把孩子提升成根，而按树形判断的实现会把它当
 * 主运行漏掉。
 *
 * 三是**说不出来的事情不要说**。`AgentDelegated` 不带子代理的目标，详情里那一块
 * 因此只讲「它被给了什么」。这一条没法用「断言某段文字不存在」钉死，能钉的是它
 * 的替代品确实在：四行事实都渲染，且工具数为 0 时说清那是交集为空而不是缺数据。
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EventEnvelope } from "../../api/types";
import { buildRunTree } from "../../components/runTree";
import { AgentEntryLine, AgentPanel } from "./AgentPanel";

const PARENT = "run_parent";
const A = "run_a";
const B = "run_b";

function event(
  runId: string,
  eventType: string,
  sequence: number,
  payload: Record<string, unknown> = {},
  timestamp = "2026-08-29T12:00:00Z",
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `evt_${String(sequence)}`,
    stream_id: "thr_1",
    run_id: runId,
    event_type: eventType,
    durability: "durable",
    timestamp,
    payload: { kind: eventType, ...payload },
    sequence,
    task_id: "task_1",
    graph_node_id: "research_internal",
    parent_event_id: null,
  };
}

function usage(input: number, output: number, toolCalls = 0) {
  return {
    steps: 0,
    tool_calls: toolCalls,
    tokens: {
      input_tokens: input,
      output_tokens: output,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    },
    cost_micro_usd: 0,
  };
}

function budget(maxTotalTokens: number | null = null) {
  return {
    max_steps: 40,
    max_tool_calls: 20,
    max_total_tokens: maxTotalTokens,
    max_cost_micro_usd: null,
    deadline: null,
  };
}

/** 一次委派：父亲宣告，孩子开跑。 */
function delegation(child: string, name: string, seq: number): EventEnvelope[] {
  return [
    event(PARENT, "AgentDelegated", seq, {
      child_agent_run_id: child,
      profile_name: name,
    }),
  ];
}

function draw(events: EventEnvelope[], openRunId: string | null = null) {
  const onOpen = vi.fn();
  const onClose = vi.fn();
  const onInspect = vi.fn();
  const view = render(
    <AgentPanel
      events={events}
      onClose={onClose}
      onInspect={onInspect}
      onOpen={onOpen}
      openRunId={openRunId}
      roots={buildRunTree(events)}
    />,
  );
  return { ...view, onOpen, onClose, onInspect };
}

describe("集合层：一屏读完谁怎么样了", () => {
  it("按状态分组，要动手的那一组排在最前", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "analyst", 2),
      ...delegation(B, "critic", 3),
      event(A, "RunStarted", 4, { budget: budget(120_000) }),
      event(A, "ModelStarted", 5),
      event(B, "RunStarted", 6, { budget: budget(120_000) }),
      event(B, "RunFailed", 7, {
        usage: usage(20_000, 1_400),
        error: { code: "timeout", message: "model call exceeded 120.0s" },
      }),
    ];
    const { container } = draw(events);

    const groups = [...container.querySelectorAll(".aw-agent-group")].map(
      (node) => node.textContent ?? "",
    );
    expect(groups[0]).toContain("失败");
    expect(groups[1]).toContain("进行中");
  });

  it("跑完的子代理不再重复一句「已完成」——状态那一列已经说过了", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "summarizer", 2),
      event(A, "RunStarted", 3, { budget: budget(120_000) }),
      event(A, "RunCompleted", 4, { usage: usage(40_000, 4_900, 2) }),
    ];
    const { container } = draw(events);

    const name = container.querySelector(".aw-agent-name");
    expect(name?.textContent).toBe("summarizer");
  });

  it("失败的那一条把失败原因写在名字旁边，而不是只标一个红点", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "critic", 2),
      event(A, "RunStarted", 3, { budget: budget(120_000) }),
      event(A, "RunFailed", 4, {
        usage: usage(20_000, 1_400),
        error: { code: "timeout", message: "model call exceeded 120.0s" },
      }),
    ];
    const { container } = draw(events);

    // 断言的是「失败原因和名字在同一行、并且是红的」，不是某一个具体的数字：
    // 那句话由 `explainRunFailure` 决定，它会随服务端措辞变，而这一行要钉的是
    // 「失败原因没有被折进详情里」这件事。
    const bad = container.querySelector(".aw-agent-name span.is-bad");
    expect(bad).not.toBeNull();
    expect(bad?.textContent?.length ?? 0).toBeGreaterThan(0);
  });
});

describe("没有分母的时候不画分数", () => {
  it("声明了上限的运行画额度条", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "analyst", 2),
      event(A, "RunStarted", 3, { budget: budget(120_000) }),
      event(A, "RunCompleted", 4, { usage: usage(60_000, 0) }),
    ];
    const { container } = draw(events);

    expect(container.querySelector(".aw-agent-meter")).not.toBeNull();
    expect(screen.getByText(/60\.0k\/120\.0k/)).toBeInTheDocument();
  });

  it("没有上限的运行只写花掉的那个数，不画条也不写 /0", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "analyst", 2),
      event(A, "RunStarted", 3, { budget: budget(null) }),
      event(A, "RunCompleted", 4, { usage: usage(60_000, 0) }),
    ];
    const { container } = draw(events);

    expect(container.querySelector(".aw-agent-meter")).toBeNull();
    expect(container.textContent).not.toContain("/0");
  });
});

describe("父亲不在这一页时，孩子仍然是子代理", () => {
  it("委派事件在、派它的运行自己的事件不在，孩子照样出现在集合里", () => {
    // `buildRunTree` 会把这个孩子提升成根（它的父亲不在表里），而它的
    // `parentRunId` 仍然写着——这一条钉的就是「按字段判断而不是按树形判断」。
    const events = [
      ...delegation(A, "analyst", 1),
      event(A, "RunStarted", 2, { budget: budget(120_000) }),
      event(A, "RunCompleted", 3, { usage: usage(10_000, 500) }),
    ];
    const { container } = draw(events);

    expect(container.querySelector(".aw-agent-name")?.textContent).toContain(
      "analyst",
    );
  });
});

describe("详情层：进去才付那个篇幅", () => {
  const events = [
    event(PARENT, "RunStarted", 1, { budget: budget() }),
    ...delegation(A, "critic", 2),
    event(A, "RunStarted", 3, { budget: budget(120_000), model_profile: "deep" }),
    event(A, "ToolStarted", 4, { tool_name: "fs.read" }),
    event(A, "ToolCompleted", 5, { tool_name: "fs.read" }),
    event(A, "RunFailed", 6, {
      usage: usage(20_000, 1_400, 3),
      error: { code: "timeout", message: "model call exceeded 120.0s" },
    }),
  ];

  it("列出它做过什么，带上工具名", () => {
    draw(events, A);

    const log = screen.getByRole("heading", { name: "它做过什么" })
      .parentElement as HTMLElement;
    // 开始和返回各一行，两行都带工具名——这正是带上它的理由：不带的话这两行
    // 长得一模一样。
    expect(within(log).getAllByText(/fs\.read/)).toHaveLength(2);
    expect(within(log).getByText("正在调用工具 fs.read")).toBeInTheDocument();
  });

  it("四行事实都在，且工具数为 0 时说清那是交集为空", () => {
    const noTools = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "researcher", 2),
      event(A, "RunStarted", 3, {
        budget: budget(120_000),
        // 空的工具表，不是缺字段：`runTree` 读的是 `tool_names.length`。
        tool_names: [],
      }),
      event(A, "RunCompleted", 4, { usage: usage(1_000, 100) }),
    ];
    draw(noTools, A);

    const facts = screen.getByRole("heading", { name: "它被派出去时拿到的" })
      .parentElement as HTMLElement;
    expect(within(facts).getByText("派它的")).toBeInTheDocument();
    expect(within(facts).getByText("token 上限")).toBeInTheDocument();
    expect(within(facts).getByText(/交集可以是空的|一个都没有/)).toBeInTheDocument();
  });

  it("「不是额度用尽」只在还剩得下的时候说", () => {
    draw(events, A);
    expect(screen.getByText(/不是额度用尽/)).toBeInTheDocument();
  });

  it("烧满了额度的失败不说「不是额度用尽」——那时它可能正是", () => {
    const burnt = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "critic", 2),
      event(A, "RunStarted", 3, { budget: budget(120_000) }),
      event(A, "RunFailed", 4, {
        usage: usage(119_000, 900),
        error: { code: "budget", message: "token budget exhausted" },
      }),
    ];
    draw(burnt, A);

    expect(screen.queryByText(/不是额度用尽/)).toBeNull();
  });

  it("返回退回集合，而不是关掉整块面", async () => {
    const { onOpen } = draw(events, A);
    await userEvent.click(screen.getByRole("button", { name: "回到子代理列表" }));
    expect(onOpen).toHaveBeenCalledWith(null);
  });

  it("「只看这个运行的记录」把步骤流收窄到它——这是这块面唯一敢承诺的动作", async () => {
    const { onInspect } = draw(events, A);
    await userEvent.click(
      screen.getByRole("button", { name: "只看这个运行的记录" }),
    );
    expect(onInspect).toHaveBeenCalledWith(A);
  });
});

describe("正文里那一行入口", () => {
  it("没有子代理时整行不渲染", () => {
    const { container } = render(
      <AgentEntryLine
        incomplete={false}
        onOpen={vi.fn()}
        open={false}
        roots={buildRunTree([
          event(PARENT, "RunStarted", 1, { budget: budget() }),
          event(PARENT, "RunCompleted", 2, { usage: usage(10, 10) }),
        ])}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("有子代理时报出个数、在跑与失败的分解，以及合计", () => {
    const events = [
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      ...delegation(A, "analyst", 2),
      ...delegation(B, "critic", 3),
      event(A, "RunStarted", 4, { budget: budget(120_000) }),
      event(A, "ModelStarted", 5),
      event(B, "RunStarted", 6, { budget: budget(120_000) }),
      event(B, "RunFailed", 7, {
        usage: usage(20_000, 1_400),
        error: { code: "timeout", message: "model call exceeded 120.0s" },
      }),
    ];
    render(
      <AgentEntryLine
        incomplete={false}
        onOpen={vi.fn()}
        open={false}
        roots={buildRunTree(events)}
      />,
    );

    expect(screen.getByText("子代理 2 个")).toBeInTheDocument();
    expect(screen.getByText("1 个在跑 · 1 个失败")).toBeInTheDocument();
    expect(screen.getByText(/不占这个任务的额度/)).toBeInTheDocument();
  });
});

describe("流不完整时，「没有子代理」这个答案不许沉默地给出", () => {
  it("一个没有子代理、但分页有缺口的任务仍然说出这一点", () => {
    render(
      <AgentEntryLine
        incomplete
        onOpen={vi.fn()}
        open={false}
        roots={buildRunTree([
          event(PARENT, "RunStarted", 1, { budget: budget() }),
          event(PARENT, "RunCompleted", 2, { usage: usage(10, 10) }),
        ])}
      />,
    );

    expect(screen.getByText(/可能不全/)).toBeInTheDocument();
  });
});
