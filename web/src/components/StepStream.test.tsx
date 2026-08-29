/**
 * 渲染 `StepStream`。
 *
 * 这个组件被 Work 与 Chat 共用，而在这份文件之前它**一条渲染测试都没有**——
 * `stepGroups`、`activityPresentation`、`runTree` 各有自己的纯函数测试，组件本身
 * 一个断言都没有。于是「改共用组件」这件事在这个仓库里一直被估得很贵：不是因为
 * 它难，是因为改完没有任何东西会告诉你它还对不对。
 *
 * 所以这里先钉的是**它现在的行为**，而不是新加的东西：阶段怎么排、什么时候展开、
 * 一个步骤组折不折、空阶段长什么样、`meta` 那一折在不在。新功能（按运行分段）的
 * 测试排在后面，读起来是「在这些不变的行为之上又多了一层」。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import { StepStream, type StreamStage } from "./StepStream";

const PARENT = "run_parent";
const CHILD = "run_child";

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
    timestamp: "2026-08-29T04:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence,
    task_id: "task_1",
    graph_node_id: "work",
    parent_event_id: null,
  };
}

/** 事件类型直接当标题用，测试里读起来就是它自己。 */
const title = (e: EventEnvelope) => e.event_type;

function stage(overrides: Partial<StreamStage> = {}): StreamStage {
  return {
    id: "work",
    title: "执行",
    state: "done",
    note: "12 秒",
    events: [event(PARENT, "RunStarted", 1)],
    ...overrides,
  };
}

function draw(stages: StreamStage[], props: Record<string, unknown> = {}) {
  return render(
    <StepStream
      ariaLabel="执行过程"
      eventTitle={title}
      running={false}
      stages={stages}
      {...props}
    />,
  );
}

describe("阶段", () => {
  it("每个阶段一行，按传进来的顺序", () => {
    draw([
      stage({ id: "a", title: "理解" }),
      stage({ id: "b", title: "执行" }),
      stage({ id: "c", title: "复核" }),
    ]);

    const rows = screen.getAllByRole("listitem");
    const titles = rows
      .map((row) => row.querySelector(".aw-stream-title")?.textContent)
      .filter((t): t is string => t !== undefined && t !== null);
    expect(titles).toEqual(["理解", "执行", "复核"]);
  });

  it("没有事件的阶段不给折叠控件——没有东西可以打开", () => {
    const { container } = draw([stage({ events: [] })]);

    expect(container.querySelector(".aw-stream-head.is-empty")).not.toBeNull();
    expect(container.querySelector("details.aw-stream-step")).toBeNull();
  });

  it("跑着的时候只有活动中的那个阶段是展开的", () => {
    // 一个跑完的运行折成每阶段一行；正在动的那个自己打开。这条钉的是「只有
    // 一个」——两个同时展开等于没有展开。
    const { container } = draw(
      [
        stage({ id: "a", title: "理解", state: "done" }),
        stage({ id: "b", title: "执行", state: "active" }),
        stage({ id: "c", title: "复核", state: "pending" }),
      ],
      { running: true },
    );

    const open = [...container.querySelectorAll("details.aw-stream-step")].filter(
      (d) => (d as HTMLDetailsElement).open,
    );
    expect(open).toHaveLength(1);
    expect(open[0]?.querySelector(".aw-stream-title")?.textContent).toBe("执行");
  });

  it("停下来之后一个阶段都不展开", () => {
    const { container } = draw([
      stage({ id: "a", state: "done" }),
      stage({ id: "b", state: "active" }),
    ]);

    const open = [...container.querySelectorAll("details.aw-stream-step")].filter(
      (d) => (d as HTMLDetailsElement).open,
    );
    expect(open).toEqual([]);
  });
});

describe("步骤组", () => {
  it("一次工具调用折成一行，展开才看得到它的几条事件", () => {
    const events = [
      event(PARENT, "ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "knowledge_search",
      }),
      event(PARENT, "ToolStarted", 2, {
        tool_call_id: "call_1",
        tool_name: "knowledge_search",
      }),
      event(PARENT, "ToolCompleted", 3, { tool_call_id: "call_1" }),
    ];
    const { container } = draw([stage({ events })]);

    const groups = container.querySelectorAll("details.aw-step-group");
    expect(groups).toHaveLength(1);
    // 折起来的意思是 `<details>` 没有 open——不是「内容不在 DOM 里」。原生
    // `<details>` 的子节点一直在文档里，只是不显示；断言「查不到那段文字」会在
    // 一个把 open 写死成 true 的实现上照样通过。
    expect((groups[0] as HTMLDetailsElement).open).toBe(false);
    expect(
      within(groups[0] as HTMLElement).getAllByRole("listitem"),
    ).toHaveLength(3);
  });

  it("只有一条事件的组不套第二层折叠", () => {
    // 套了的话，一行前面会出现两个折叠三角。
    const { container } = draw([
      stage({ events: [event(PARENT, "ContextBuilt", 1)] }),
    ]);

    expect(container.querySelector("details.aw-step-group")).toBeNull();
    expect(container.querySelector("details.aw-step")).not.toBeNull();
  });

  it("进行中的组自己是展开的", () => {
    const events = [
      event(PARENT, "ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "knowledge_search",
      }),
      event(PARENT, "ToolStarted", 2, {
        tool_call_id: "call_1",
        tool_name: "knowledge_search",
      }),
    ];
    const { container } = draw([stage({ state: "active", events })], {
      running: true,
    });

    const group = container.querySelector("details.aw-step-group");
    expect((group as HTMLDetailsElement | null)?.open).toBe(true);
  });

  it("一条事件都不丢：进去几条，展开之后还是几条", () => {
    // `stepGroups` 的核心承诺是「折叠，不丢弃」。它自己有纯函数测试，这里钉的是
    // 渲染这一层也没有把谁吞掉。
    const events = [
      event(PARENT, "ModelStarted", 1, { model_call_id: "m1" }),
      event(PARENT, "ModelCompleted", 2, { model_call_id: "m1" }),
      event(PARENT, "ToolProposed", 3, {
        tool_call_id: "call_1",
        tool_name: "sandbox_run",
      }),
      event(PARENT, "ToolCompleted", 4, { tool_call_id: "call_1" }),
      event(PARENT, "RunCompleted", 5),
    ];
    const { container } = draw([stage({ events })]);

    container.querySelectorAll("details").forEach((d) => {
      (d).open = true;
    });
    expect(container.querySelectorAll("details.aw-step")).toHaveLength(
      events.length,
    );
  });
});

describe("实时那一句", () => {
  it("跑着的时候有，停下来就没有", () => {
    const events = [event(PARENT, "ToolStarted", 1, { tool_name: "web_fetch" })];

    const live = draw([stage({ state: "active", events })], { running: true });
    expect(live.container.querySelector(".aw-live-activity")).not.toBeNull();
    live.unmount();

    const still = draw([stage({ state: "done", events })]);
    expect(still.container.querySelector(".aw-live-activity")).toBeNull();
  });
});

describe("meta 那一折", () => {
  it("有事件才画", () => {
    const withMeta = draw([stage()], {
      meta: { title: "任务记录", events: [event("task_1", "TaskSubmitted", 9)] },
    });
    expect(withMeta.container.querySelector(".aw-stream-meta")).not.toBeNull();
    withMeta.unmount();

    const empty = draw([stage()], { meta: { title: "任务记录", events: [] } });
    expect(empty.container.querySelector(".aw-stream-meta")).toBeNull();
  });
});

describe("认不出来的事件带着记号", () => {
  it("`isKnownEvent` 说不认识的那一条挂 is-unknown", () => {
    const events = [
      event(PARENT, "ContextBuilt", 1),
      event(PARENT, "SomethingNewFromTheFuture", 2),
    ];
    const { container } = draw([stage({ events })], {
      isKnownEvent: (e: EventEnvelope) => e.event_type !== "SomethingNewFromTheFuture",
    });

    const unknown = container.querySelectorAll("li.is-unknown");
    expect(unknown).toHaveLength(1);
    expect(unknown[0]?.textContent).toContain("SomethingNewFromTheFuture");
  });
});


describe("按运行分段", () => {
  /** Work 会给的那种命名函数：认得子运行，认不得的返回 null。 */
  const label = (runId: string) =>
    runId === CHILD
      ? { title: "analyst", badge: "子代理", outcome: "done" as const, note: "36.1k" }
      : null;

  const delegated = () => [
    event(PARENT, "ContextBuilt", 1),
    event(PARENT, "AgentDelegated", 2, { child_agent_run_id: CHILD }),
    event(CHILD, "RunStarted", 3),
    event(CHILD, "ModelStarted", 4, { model_call_id: "m1" }),
    event(CHILD, "RunCompleted", 5),
    event(PARENT, "RunCompleted", 6),
  ];

  it("不给 runLabel 时一段都不分——Chat 走的就是这条", () => {
    const { container } = draw([stage({ events: delegated() })]);

    expect(container.querySelector(".aw-run-section")).toBeNull();
  });

  it("没有第二个运行的阶段照旧一列画", () => {
    // 这条决定了绝大多数任务看不出任何变化。
    const { container } = draw(
      [stage({ events: [event(PARENT, "ContextBuilt", 1), event(PARENT, "RunCompleted", 2)] })],
      { runLabel: label },
    );

    expect(container.querySelector(".aw-run-section")).toBeNull();
  });

  it("子运行的事件装进一个可折叠的块，用调用方给的名字", () => {
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: label,
    });

    const sections = container.querySelectorAll("details.aw-run-section");
    expect(sections).toHaveLength(1);
    const head = sections[0]?.querySelector(".aw-run-section-title");
    expect(head?.textContent).toBe("analyst");
    expect(sections[0]?.textContent).toContain("36.1k");
  });

  it("块里装的正是那个子运行的事件，父运行的一条都不在里面", () => {
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: label,
    });

    const section = container.querySelector("details.aw-run-section");
    const inside = within(section as HTMLElement).getAllByRole("listitem");
    const text = (section as HTMLElement).textContent ?? "";
    expect(inside.length).toBeGreaterThan(0);
    expect(text).toContain("RunStarted");
    expect(text).not.toContain("ContextBuilt");
    expect(text).not.toContain("AgentDelegated");
  });

  it("父运行在委派之后做的事仍然排在块的后面，不被挪走", () => {
    // 按 run_id 归堆会把这一条挪到子运行前面去，那是在重写发生过的顺序。
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: label,
    });

    const list = container.querySelector(".aw-stream-step > .aw-stream-events");
    const children = [...(list?.children ?? [])];
    const sectionAt = children.findIndex((li) =>
      li.querySelector(".aw-run-section"),
    );
    const lastText = children.at(-1)?.textContent ?? "";
    expect(sectionAt).toBeGreaterThanOrEqual(0);
    expect(sectionAt).toBeLessThan(children.length - 1);
    expect(lastText).toContain("RunCompleted");
  });

  it("叫不出名字的运行照样装进框里，用短 id 兜底", () => {
    // 取代「子代理 X：」前缀之后唯一不能丢的那半：一页缺失的 AgentDelegated 会让
    // 页面说不出这个子运行是谁，而把它的事件画成父运行干的是这里唯一错的答案。
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: () => null,
    });

    const section = container.querySelector("details.aw-run-section");
    expect(section).not.toBeNull();
    expect(section?.querySelector(".aw-run-section-title")?.textContent).toContain(
      "运行 ",
    );
  });

  it("跑着的子运行那一块自己是展开的", () => {
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: (runId: string) =>
        runId === CHILD
          ? { title: "analyst", badge: "子代理", outcome: "running" as const }
          : null,
    });

    const section = container.querySelector("details.aw-run-section");
    expect((section as HTMLDetailsElement | null)?.open).toBe(true);
  });

  it("跑完的子运行那一块是折起来的", () => {
    const { container } = draw([stage({ events: delegated() })], {
      runLabel: label,
    });

    const section = container.querySelector("details.aw-run-section");
    expect((section as HTMLDetailsElement | null)?.open).toBe(false);
  });

  it("一条事件都不丢：分段之后展开，还是原来那些", () => {
    const events = delegated();
    const { container } = draw([stage({ events })], { runLabel: label });

    container.querySelectorAll("details").forEach((d) => {
      (d).open = true;
    });
    expect(container.querySelectorAll("details.aw-step")).toHaveLength(
      events.length,
    );
  });

  it("父子撞了同一个 tool_call_id 也不会被折成一个组", () => {
    // 组的 key 是 `tool:${tool_call_id}`，不含 run_id。先分组再切段的话，这两次
    // 调用会折成一个组，然后整个组只能落在某一段里——一次调用凭空归给了另一个
    // agent。逐段分组时这件事不可能发生。
    const events = [
      event(PARENT, "ToolProposed", 1, { tool_call_id: "call_1", tool_name: "a" }),
      event(PARENT, "ToolCompleted", 2, { tool_call_id: "call_1" }),
      event(CHILD, "ToolProposed", 3, { tool_call_id: "call_1", tool_name: "a" }),
      event(CHILD, "ToolCompleted", 4, { tool_call_id: "call_1" }),
    ];
    const { container } = draw([stage({ events })], { runLabel: label });

    // 两次调用、两个组：一个在框外（父运行的），一个在框里（子运行的）。
    expect(container.querySelectorAll("details.aw-step-group")).toHaveLength(2);
    const section = container.querySelector("details.aw-run-section");
    expect(
      (section as HTMLElement).querySelectorAll("details.aw-step-group"),
    ).toHaveLength(1);
  });

  it("同一个子运行被穿插几次，还是一个块", () => {
    // 最初的实现是纯连续分段，这条测试当年断言的是「两个块」。拿真数据
    // （`task_75cd1e0c`，四个 analyst、并发上限 2）一跑，连续分段把四个子代理
    // 摊成了十个一两条事件的小块——并发的两个 agent 之间没有顺序可读，那十个
    // 交替的块在暗示一场并不存在的对话。所以只合并**别人的**运行，父运行的段
    // 一条不动，实测四个子代理正好四块、每块四条。
    const events = [
      event(PARENT, "ContextBuilt", 1),
      event(CHILD, "RunStarted", 2),
      event(PARENT, "ModelStarted", 3, { model_call_id: "m1" }),
      event(CHILD, "RunCompleted", 4),
    ];
    const { container } = draw([stage({ events })], { runLabel: label });

    const sections = container.querySelectorAll("details.aw-run-section");
    expect(sections).toHaveLength(1);
    // 两条子运行的事件都在这一块里。
    const text = (sections[0] as HTMLElement).textContent ?? "";
    expect(text).toContain("RunStarted");
    expect(text).toContain("RunCompleted");
  });

  it("父运行的事件不会被并进块里，顺序也不动", () => {
    // 合并只动别人的运行。父运行的事件之间有真正的先后——它委派、它等、它拿到
    // 结果、它接着做——把它们归堆才是在重写发生过的顺序。
    const events = [
      event(PARENT, "ContextBuilt", 1),
      event(CHILD, "RunStarted", 2),
      event(PARENT, "ModelStarted", 3, { model_call_id: "m1" }),
      event(CHILD, "RunCompleted", 4),
      event(PARENT, "ModelCompleted", 5, { model_call_id: "m1" }),
    ];
    const { container } = draw([stage({ events })], { runLabel: label });

    const block = container.querySelector("details.aw-run-section") as HTMLElement;
    expect(block.textContent).not.toContain("ModelStarted");
    expect(container.textContent).toContain("ModelStarted");
  });
});

describe("徽标是调用方的断言，不是组件的猜测", () => {
  const delegatedEvents = () => [
    event(PARENT, "ContextBuilt", 1),
    event(CHILD, "RunStarted", 2),
    event(PARENT, "RunCompleted", 3),
  ];

  it("给了 badge 才画", () => {
    const { container } = draw([stage({ events: delegatedEvents() })], {
      runLabel: () => ({ title: "analyst", badge: "子代理", outcome: "done" as const }),
    });

    expect(container.querySelector(".aw-run-section-badge")?.textContent).toBe(
      "子代理",
    );
  });

  it("没给 badge 就不画——同一个节点的第二次运行不是任何人的子代理", () => {
    const { container } = draw([stage({ events: delegatedEvents() })], {
      runLabel: () => ({ title: "第 2 次运行", outcome: "done" as const }),
    });

    expect(container.querySelector(".aw-run-section")).not.toBeNull();
    expect(container.querySelector(".aw-run-section-badge")).toBeNull();
    expect(container.textContent).toContain("第 2 次运行");
  });
});
