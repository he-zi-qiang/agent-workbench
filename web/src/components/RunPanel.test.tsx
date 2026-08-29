/**
 * The panel that says who is working on this Task.
 *
 * `runTree.test.ts` covers the shape the events describe; this file covers
 * what a reader is actually shown. The two were not the same thing: the tree
 * builder shipped with twelve tests and the panel with none, and most of what
 * is pinned here -- a ceiling drawn only where a run declared one, a paused run
 * that says so instead of spinning, a failure that names itself -- is a fact
 * the tree already held and the panel silently dropped.
 *
 * The last two blocks are about a different failure: not what the panel omits,
 * but what it owes a reader who cannot see anything. It holds the only control
 * that undoes a narrowing, so "there is nothing worth drawing" and "render
 * nothing" are not the same instruction.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EventEnvelope } from "../api/types";
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
  // Optional and last, so every call written before spans were rendered keeps
  // the single fixed instant it was written against -- and therefore keeps
  // showing no duration at all, which is what those tests are about.
  timestamp = "2026-08-26T12:00:00Z",
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

function draw(
  events: EventEnvelope[],
  selectedRunId: string | null = null,
  incomplete = false,
) {
  const onSelect = vi.fn();
  const view = render(
    <RunPanel
      incomplete={incomplete}
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

  it("撞输出上限的子运行说的是「一次话太长」，不是「总额用完了」", () => {
    // 这一条钉的是一行自相矛盾的界面。实测：`token 预算用尽` 配着 `17.2k/30.0k`
    // ——一个没满的分数——再配一句没翻译的英文。三段要读者自己去调和，而正确
    // 的读法是：撞的根本不是这里显示的那个上限。
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, {
          budget: budget({ max_total_tokens: 30_000 }),
        }),
        event(CHILD, "RunFailed", 4, {
          stop_reason: "token_budget",
          error: {
            code: "budget_exceeded",
            message: "the model stopped at its output token ceiling",
          },
          usage: usage(900, 16_384),
        }),
      ),
    );

    expect(screen.getByText(/单次回答/)).toBeInTheDocument();
    // 那句没翻译的英文原话不该再出现在行上。
    expect(screen.queryByText(/output token ceiling/)).not.toBeInTheDocument();
  });

  it("没学过的失败句子仍然原样显示服务端的原话", () => {
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

describe("一棵可能不全的树要自己说出来", () => {
  /**
   * ADR-083 不变量 5「一棵不完整的树说自己不完整」此前只做了服务端那一半——
   * 响应里有 `complete`，客户端没有对应物。而客户端这棵树恰恰更需要它：它是从
   * 页面握着的事件重建的，一页没送到、里面又刚好有一次 `AgentDelegated`，
   * 整条分支就不在树里，而且**什么痕迹都不留**。
   */
  it("流有缺口时面板说这棵树可能不全", () => {
    draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
      null,
      true,
    );

    expect(screen.getByText(/可能不全/)).toBeInTheDocument();
  });

  it("流完整时不说这句话——沉默在这里必须是有意义的", () => {
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })));

    expect(screen.queryByText(/可能不全/)).not.toBeInTheDocument();
  });
});

describe("面板握着退出收窄的唯一出口，所以收窄活着时它必须在", () => {
  /**
   * 这一组钉的是把收窄搬进 URL 之后暴露的那个陷阱。`?run=` 可以指向一个这个
   * 任务里根本没有的运行——一条发错的链接，或者一次落在缺口里的委派。而面板
   * 原来在「没发生过委派」时直接 return null，于是读者拿到的是一条空的执行
   * 过程、没有任何解释，也没有任何能点回去的东西。
   */
  it("任务没派过子代理、但收窄开着时，面板仍然渲染并给出退路", async () => {
    const user = userEvent.setup();
    const { onSelect } = draw(
      [
        event(PARENT, "RunStarted", 1, { budget: budget() }),
        event(PARENT, "RunCompleted", 2, { usage: usage(10, 10) }),
      ],
      "run_not_here",
    );

    expect(screen.getByText(/不在这条流里的运行/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "显示全部" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("收窄指向树里真有的运行时，说的是另一句话", () => {
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })), CHILD);

    expect(screen.getByText(/只显示这一个运行/)).toBeInTheDocument();
    expect(screen.queryByText(/不在这条流里/)).not.toBeInTheDocument();
  });

  it("收窄到一个树里没有的运行，即使有子代理也照实说", () => {
    draw(
      delegated(event(CHILD, "RunStarted", 3, { budget: budget() })),
      "run_not_here",
    );

    expect(screen.getByText(/不在这条流里的运行/)).toBeInTheDocument();
    // 树还在，读者可以直接点另一行换过去。
    expect(screen.getByText("analyst")).toBeInTheDocument();
  });

  it("既没有子代理也没有收窄时，面板仍然不出现", () => {
    const { container } = draw([
      event(PARENT, "RunStarted", 1, { budget: budget() }),
      event(PARENT, "RunCompleted", 2, { usage: usage(10, 10) }),
    ]);

    expect(container).toBeEmptyDOMElement();
  });
});

describe("跑了多久，和一副空工具箱", () => {
  it("一个运行的跨度画在它自己那一行上", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget() }, "2026-08-26T12:00:00Z"),
        event(
          CHILD,
          "RunCompleted",
          4,
          { usage: usage(10, 10), stop_reason: "completed" },
          "2026-08-26T12:00:45Z",
        ),
      ),
    );

    expect(screen.getByText("45 秒")).toBeInTheDocument();
  });

  it("只有一条事件的运行不说自己跑了 0 秒", () => {
    // 「0 秒」读起来是一个什么都没干的运行。它其实是一个还没写到第二条事件的
    // 运行，而这两件事读者要做的下一步不一样。
    draw(delegated(event(CHILD, "RunStarted", 3, { budget: budget() })));

    expect(screen.queryByText(/秒$/)).toBeNull();
    expect(screen.queryByText(/分钟$/)).toBeNull();
  });

  it("拿到空工具箱的子代理会说出来", () => {
    // 子代理的工具是「它自己的上限 ∩ 这个任务的授权信封」。一个没有授权知识库
    // 检索的任务照样派得出 researcher，只是它拿着空工具集回来说什么都查不到——
    // `application/sub_agents.py` 把这条写成了有意的取舍，而它此前在界面上完全
    // 看不见：运行开始、一个工具都不调、报告说没找到。
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, { budget: budget(), tool_names: [] }),
      ),
    );

    expect(screen.getByText(/一件工具都没拿到/)).toBeInTheDocument();
  });

  it("拿到了工具的子代理不说这句话", () => {
    draw(
      delegated(
        event(CHILD, "RunStarted", 3, {
          budget: budget(),
          tool_names: ["knowledge_search"],
        }),
      ),
    );

    expect(screen.queryByText(/一件工具都没拿到/)).toBeNull();
  });

  it("没有工具的图节点不算空工具箱", () => {
    // `understand` 这类节点本来就只是一次模型调用。给它们挂上同一句警告，等于
    // 在每个从没委派过的任务里给多数行加一条不该有的告警——所以这句话只对
    // 被委派出来的运行成立。
    // 手写而不是用 `delegated()`：那个 helper 已经替父运行发过一条 RunStarted，
    // 再补一条同序号的会变成「同一个运行说了两次」——测试还是绿的，但绿的原因
    // 就不再是这条规则了。
    draw([
      event(PARENT, "RunStarted", 1, { budget: budget(), tool_names: [] }),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 3, {
        budget: budget(),
        tool_names: ["knowledge_search"],
      }),
    ]);

    // 父运行确实拿到了空工具箱——这一条在，上面那句断言才不是空跑。
    expect(screen.getByText("work")).toBeInTheDocument();
    expect(screen.queryByText(/一件工具都没拿到/)).toBeNull();
  });
});
