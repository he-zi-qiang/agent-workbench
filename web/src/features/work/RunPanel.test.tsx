/**
 * The panel that says who is working on this Task.
 *
 * `runTree.test.ts` covers the shape the events describe; this file covers
 * what a reader is actually shown. The two were not the same thing: the tree
 * builder shipped with thirteen tests and the panel with none, and everything
 * pinned here -- a ceiling drawn only where a run declared one, a paused run
 * that says so instead of spinning, a failure that names itself -- is a fact
 * the tree already held and the panel silently dropped.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EventEnvelope } from "../../api/types";
import { RunPanel } from "./RunPanel";
import { buildRunTree } from "./runTree";

const PARENT = "run_parent";
const CHILD = "run_child";
const SECOND = "run_second";

function event(
  runId: string,
  eventType: string,
  sequence: number,
  payload: Record<string, unknown> = {},
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `evt_${String(sequence)}`,
    stream_id: "thr_1",
    run_id: runId,
    event_type: eventType,
    durability: "durable",
    timestamp: "2026-08-26T12:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence,
    task_id: "task_1",
    graph_node_id: "work",
    parent_event_id: null,
  };
}

function usage(input: number, output: number, extra: Record<string, number> = {}) {
  return {
    steps: extra.steps ?? 0,
    tool_calls: extra.tool_calls ?? 0,
    tokens: {
      input_tokens: input,
      output_tokens: output,
      cache_read_tokens: 0,
      cache_write_tokens: extra.cache_write_tokens ?? 0,
    },
    cost_micro_usd: 0,
  };
}

/** A budget shaped like the one a delegated run is actually given. */
function budget(overrides: Record<string, unknown> = {}) {
  return {
    max_steps: 40,
    max_tool_calls: 20,
    max_total_tokens: null,
    max_cost_micro_usd: null,
    deadline: null,
    ...overrides,
  };
}

/** The smallest stream that makes the panel render at all: one delegation. */
function delegated(...extra: EventEnvelope[]): EventEnvelope[] {
  return [
    event(PARENT, "RunStarted", 1, { budget: budget(), model_profile: "main" }),
    event(PARENT, "AgentDelegated", 2, {
      child_agent_run_id: CHILD,
      profile_name: "analyst",
    }),
    ...extra,
  ];
}

function draw(events: EventEnvelope[], selectedRunId: string | null = null) {
  const onSelect = vi.fn();
  const view = render(
    <RunPanel
      onSelect={onSelect}
      roots={buildRunTree(events)}
      selectedRunId={selectedRunId}
    />,
  );
  return { ...view, onSelect };
}

describe("面板只在真的发生过委派时出现", () => {
  it("没有子代理的任务不渲染这个面板", () => {
    const { container } = draw([
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      event(PARENT, "RunCompleted", 2, { usage: usage(10, 10) }),
    ]);

    expect(container).toBeEmptyDOMElement();
  });

  it("委派刚写下、子运行还没说话时面板就已经在了", () => {
    draw(delegated());

    expect(screen.getByRole("region", { name: "参与这次任务的 Agent" })).toBeInTheDocument();
    expect(screen.getByText("analyst")).toBeInTheDocument();
    expect(screen.getByText(/1 个是子代理/)).toBeInTheDocument();
  });
});

describe("嵌套是这个面板存在的理由，所以它必须在 DOM 里", () => {
  it("子代理是父运行那一行的后代，而不是它的兄弟", () => {
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })));

    const parentRow = screen.getByText("work").closest("li");
    expect(parentRow).not.toBeNull();
    // 子代理能在父行的**内部**找到——缩进是样式，这一条是结构。
    expect(within(parentRow as HTMLElement).getByText("analyst")).toBeInTheDocument();
  });

  it("有子代理的行可以折叠，折叠后子代理不在文档里", async () => {
    const user = userEvent.setup();
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })));

    const toggle = screen.getByRole("button", { name: /折叠 work 派生的子代理/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.click(toggle);

    expect(screen.queryByText("analyst")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /展开 work 派生的子代理/ }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("没有子代理的行不给折叠按钮", () => {
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })));

    // 父运行一个、子运行零个。
    expect(screen.getAllByRole("button", { name: /派生的子代理/ })).toHaveLength(1);
  });

  it("面板还开着的时候新到的子代理不会被折叠状态藏掉", async () => {
    const user = userEvent.setup();
    const { rerender, onSelect } = draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
    );

    await user.click(screen.getByRole("button", { name: /折叠 work/ }));
    expect(screen.queryByText("analyst")).not.toBeInTheDocument();

    // 折叠记的是「被合上的那些」，不是「被展开的那些」——所以后到的
    // 第二个子代理仍然会出现，而不是继承一份默认隐藏。
    rerender(
      <RunPanel
        onSelect={onSelect}
        roots={buildRunTree([
          ...delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
          event(CHILD, "AgentDelegated", 4, {
            child_agent_run_id: SECOND,
            profile_name: "reviewer",
          }),
        ])}
        selectedRunId={null}
      />,
    );

    // 被合上的仍然合着，它自己的孩子当然也看不见。
    expect(screen.queryByText("reviewer")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开 work/ }));
    expect(screen.getByText("analyst")).toBeInTheDocument();
    expect(screen.getByText("reviewer")).toBeInTheDocument();
  });
});

describe("上限来自运行自己，所以只在它声明过的时候画", () => {
  it("声明了 token 上限的子运行显示的是花费与上限两个数", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, {
          budget: budget({ max_total_tokens: 120_000 }),
        }),
        event(CHILD, "RunCompleted", 4, {
          stop_reason: "stop",
          usage: usage(30_000, 2_000, { steps: 4, tool_calls: 2 }),
        }),
      ),
    );

    // 32k/120k：分母是这次运行的 RunStarted 自己写下的，不是配置里今天的值。
    expect(screen.getByText("32.0k/120.0k")).toBeInTheDocument();
    expect(screen.getByText(/4\/40/)).toBeInTheDocument();
  });

  it("没有声明 token 上限时只显示花费，不显示一个编出来的分母", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }),
        event(CHILD, "RunCompleted", 4, {
          stop_reason: "stop",
          usage: usage(1_500, 500),
        }),
      ),
    );

    expect(screen.getByText("2.0k")).toBeInTheDocument();
    expect(screen.queryByText(/2\.0k\//)).not.toBeInTheDocument();
  });

  it("token 花费按运行时判定预算的同一套算法算，含 cache_write", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, {
          budget: budget({ max_total_tokens: 10_000 }),
        }),
        event(CHILD, "RunCompleted", 4, {
          stop_reason: "stop",
          usage: usage(1_000, 500, { cache_write_tokens: 500 }),
        }),
      ),
    );

    // 2000，不是 1500：cache_write 在提示词计数之外，是唯一可加的那个缓存数字，
    // 而 max_total_tokens 判的就是含它的那个总数。
    expect(screen.getByText("2.0k/10.0k")).toBeInTheDocument();
  });
});

describe("停下来的运行要说清它为什么停", () => {
  it("失败的子运行在行下面写出错误码与原话", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }),
        event(CHILD, "RunFailed", 4, {
          stop_reason: "error",
          error: { code: "tool_timeout", message: "web_search exceeded 30s" },
          usage: usage(900, 100),
        }),
      ),
    );

    expect(
      screen.getByText("工具执行超时：web_search exceeded 30s"),
    ).toBeInTheDocument();
  });

  it("撞上天花板停下来的运行说的是天花板，而不只是「已完成」", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, {
          budget: budget({ max_total_tokens: 120_000 }),
        }),
        event(CHILD, "RunCompleted", 4, {
          stop_reason: "token_budget",
          usage: usage(119_000, 1_000),
        }),
      ),
    );

    expect(screen.getByText("token 预算用尽")).toBeInTheDocument();
  });

  it("正常答完的运行不会多出一句停止原因", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }),
        event(CHILD, "RunCompleted", 4, {
          stop_reason: "stop",
          usage: usage(100, 100),
        }),
      ),
    );

    expect(screen.queryByText(/用尽|超时|被取消/)).not.toBeInTheDocument();
  });
});

describe("等待不是在工作", () => {
  it("停在审批门上的运行说自己在等人，而不是显示成正在跑", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }),
        event(CHILD, "RunPaused", 4, { reason: "approval" }),
      ),
    );

    expect(screen.getByText("已暂停，等待你确认")).toBeInTheDocument();
  });

  it("暂停之后又动起来的运行不再写着等待", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }),
        event(CHILD, "RunPaused", 4, { reason: "approval" }),
        event(CHILD, "ToolStarted", 5),
      ),
    );

    expect(screen.queryByText(/已暂停/)).not.toBeInTheDocument();
    expect(screen.getByText("正在调用工具")).toBeInTheDocument();
  });
});

describe("选中一行会把下面的执行过程收窄到那一个运行", () => {
  it("点一行把它的 run_id 交出去", async () => {
    const user = userEvent.setup();
    const { onSelect } = draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
    );

    await user.click(screen.getByRole("button", { name: /analyst/ }));

    expect(onSelect).toHaveBeenCalledWith(CHILD);
  });

  it("再点一次同一行是取消收窄", async () => {
    const user = userEvent.setup();
    const { onSelect } = draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
      CHILD,
    );

    await user.click(screen.getByRole("button", { name: /analyst/ }));

    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("收窄着的时候有一句话说明，且能一键退出", async () => {
    const user = userEvent.setup();
    const { onSelect } = draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
      CHILD,
    );

    expect(screen.getByText(/只显示这一个运行/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "显示全部" }));

    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("折叠一行不会顺手把它选中", async () => {
    const user = userEvent.setup();
    const { onSelect } = draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
    );

    await user.click(screen.getByRole("button", { name: /折叠 work/ }));

    expect(onSelect).not.toHaveBeenCalled();
  });
});
