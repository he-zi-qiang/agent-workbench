/**
 * Rebuilding the run tree from the events the page holds.
 *
 * The three rules the panel depends on are all about **not dropping a run**,
 * because a missing row reads as work that never happened rather than as work
 * whose record is still arriving. Each has its own block below.
 *
 * The last block is the one that would otherwise rot: this file and
 * `application/run_tree.py` answer the same question for callers in opposite
 * situations, so their answers have to agree. The scenario there is the
 * scenario here.
 */

import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import {
  buildRunTree,
  flattenRuns,
  runDurationMs,
  totalSpend,
  totalTokens,
  type RunNode,
} from "./runTree";

/**
 * The node at `index`, or a failure that names what was missing.
 *
 * `noUncheckedIndexedAccess` is on, and the alternative -- `?.` on every
 * assertion -- turns "the child is missing" into `expect(undefined).toBe(...)`,
 * which reads as the wrong value rather than as the absent row.
 */
function at(nodes: readonly RunNode[], index: number): RunNode {
  const node = nodes[index];
  if (node === undefined) {
    throw new Error(`expected a run at index ${String(index)}, found none`);
  }
  return node;
}

const TASK = "task_1";
const PARENT = "run_parent";
const CHILD = "run_child";

function event(
  runId: string,
  eventType: string,
  sequence: number,
  payload: Record<string, unknown> = {},
  nodeId: string | null = "work",
  // Last and optional so every existing call keeps the one fixed instant they
  // were written against: the timestamps only matter to the block that tests
  // them, and giving the rest a moving clock would make them depend on a fact
  // they are not about.
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
    task_id: TASK,
    graph_node_id: nodeId,
    parent_event_id: null,
  };
}

function usage(input: number, output: number, steps = 1, toolCalls = 0) {
  return {
    steps,
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

describe("一次委派就是一棵两层的树", () => {
  it("子运行挂在派生它的那个运行下面", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 3),
      event(CHILD, "RunCompleted", 4, { usage: usage(170, 182) }),
      event(PARENT, "AgentCompleted", 5, {
        child_agent_run_id: CHILD,
        status: "completed",
        usage: usage(170, 182),
      }),
      event(PARENT, "RunCompleted", 6, { usage: usage(17608, 2497, 6, 6) }),
    ]);

    expect(tree).toHaveLength(1);
    expect(at(tree, 0).runId).toBe(PARENT);
    expect(at(tree, 0).nodeId).toBe("work");

    const child = at(at(tree, 0).children, 0);
    expect(child.runId).toBe(CHILD);
    expect(child.definitionName).toBe("analyst");
    expect(child.status).toBe("completed");
    expect(child.spend.inputTokens).toBe(170);
  });

  it("父运行报的是它自己的花费，不含子运行的", () => {
    // A total is something the panel computes and shows as a total. Folding it
    // in here would suggest the parent's own budget saw those tokens, and it
    // did not (ADR-082 §5).
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunCompleted", 3, { usage: usage(170, 182) }),
      event(PARENT, "RunCompleted", 4, { usage: usage(17608, 2497) }),
    ]);

    expect(at(tree, 0).spend.inputTokens).toBe(17608);
    expect(totalSpend(tree).inputTokens).toBe(17608 + 170);
  });
});

describe("规则一：跑着的运行不能被漏掉", () => {
  it("只有 RunStarted 的运行显示为进行中", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "ModelStarted", 2),
    ]);

    expect(at(tree, 0).status).toBe("running");
    expect(at(tree, 0).latestEventType).toBe("ModelStarted");
  });
});

describe("规则二：被宣布过的孩子就算还没说话也是一个节点", () => {
  it("委派已写、子运行未写时，孩子已经在树里", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
    ]);

    const child = at(at(tree, 0).children, 0);
    expect(child.status).toBe("unknown");
    expect(child.definitionName).toBe("analyst");
    expect(child.eventCount).toBe(0);
  });

  it("父运行的转述只在孩子自己没报告时才采用", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 1, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunFailed", 2, { usage: usage(80, 10) }),
      event(PARENT, "AgentCompleted", 3, {
        child_agent_run_id: CHILD,
        status: "completed",
        usage: usage(999, 999),
      }),
    ]);

    // 第一手记录赢：孩子自己说它失败了。
    expect(at(at(tree, 0).children, 0).status).toBe("failed");
    expect(at(at(tree, 0).children, 0).spend.inputTokens).toBe(80);
  });
});

describe("规则三：不是每个 run_id 都是一个运行", () => {
  it("任务自身的生命周期事件不会变成一个运行", () => {
    const tree = buildRunTree([
      event(TASK, "TaskSubmitted", 1, {}, null),
      event(TASK, "TaskClaimed", 2, {}, null),
      event(PARENT, "RunStarted", 3),
      event(PARENT, "RunCompleted", 4, { usage: usage(10, 10) }),
      event(TASK, "TaskSucceeded", 5, {}, null),
    ]);

    expect(tree.map((node) => node.runId)).toEqual([PARENT]);
  });

  it("只写过一次委派的运行仍然是一个运行", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 9, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
    ]);

    expect(tree.map((node) => node.runId)).toEqual([PARENT]);
  });
});

describe("扇出与嵌套", () => {
  it("同一回合派出的三个子运行都在，且保持宣布顺序", () => {
    const events = [event(PARENT, "RunStarted", 1)];
    ["run_a", "run_b", "run_c"].forEach((runId, index) => {
      events.push(
        event(PARENT, "AgentDelegated", 2 + index, {
          child_agent_run_id: runId,
          profile_name: "analyst",
        }),
      );
    });

    const tree = buildRunTree(events);

    expect(at(tree, 0).children.map((node) => node.runId)).toEqual([
      "run_a",
      "run_b",
      "run_c",
    ]);
    expect(flattenRuns(tree)).toHaveLength(4);
  });

  it("孙子挂在它自己的父亲下面，而不是被拍平", () => {
    const grandchild = "run_grandchild";
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 3),
      event(CHILD, "AgentDelegated", 4, {
        child_agent_run_id: grandchild,
        profile_name: "researcher",
      }),
    ]);

    expect(tree).toHaveLength(1);
    expect(at(at(tree, 0).children, 0).children.map((n) => n.runId)).toEqual([
      grandchild,
    ]);
  });

  it("父运行不在这一页时，子运行是这一页的根", () => {
    const tree = buildRunTree([
      event(CHILD, "RunStarted", 30),
      event(CHILD, "RunCompleted", 31, { usage: usage(50, 50) }),
    ]);

    expect(tree.map((node) => node.runId)).toEqual([CHILD]);
  });

  it("空的时间线是一棵空树", () => {
    expect(buildRunTree([])).toEqual([]);
  });
});

describe("与服务端读模型的一致性", () => {
  it("同一段事件在两侧得到同一棵树", () => {
    // 与 tests/application/test_run_tree.py::test_the_child_is_a_child_and_
    // the_parent_is_a_root 是同一个场景。两侧回答的是同一个问题，只是调用方
    // 处境不同——这条断言是它们不许分家的地方。
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 3),
      event(CHILD, "RunCompleted", 4, { usage: usage(100, 0, 2) }),
      event(PARENT, "AgentCompleted", 5, {
        child_agent_run_id: CHILD,
        status: "completed",
        usage: usage(100, 0, 2),
      }),
      event(PARENT, "RunCompleted", 6, { usage: usage(300, 0, 2) }),
    ]);

    expect(tree).toHaveLength(1);
    const root = at(tree, 0);
    expect(root.parentRunId).toBeNull();
    expect(root.definitionName).toBeNull();
    expect(root.children.map((n) => n.runId)).toEqual([CHILD]);
    const only = at(root.children, 0);
    expect(only.parentRunId).toBe(PARENT);
    expect(only.definitionName).toBe("analyst");
    expect(only.status).toBe("completed");
    expect(only.spend.inputTokens).toBe(100);
    expect(root.spend.inputTokens).toBe(300);
  });
});

describe("二手转述：父运行说的话在孩子自己没说话时必须解得开", () => {
  /**
   * 这一组钉的是一个真发生过的 bug。
   *
   * `AgentCompleted.status` 是一个 `RunStatus`——`completed`/`failed`/
   * `cancelled`（`domain/runs.py:35`）——而第一版把它拿去查了那张按**事件名**
   * （`RunCompleted`/`RunFailed`/`RunCancelled`）建的表。三个键一个都对不上，
   * 于是每次都落到 `unknown`：`AgentCompleted` 存在的唯一理由——让只握着父运行
   * 那一页的读者知道孩子怎么样了——整条路径静默失效，行上永远写着「等待中」。
   *
   * 原来那条「父运行的转述只在孩子自己没报告时才采用」测的是孩子**说过话**的
   * 那一支，恰好是两边行为相同、也是坏掉的分支走不到的那一支。
   */
  it("孩子没留下自己的终结事件时，采用父运行说的 completed", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(PARENT, "AgentCompleted", 3, {
        child_agent_run_id: CHILD,
        status: "completed",
        stop_reason: "stop",
        usage: usage(120, 30),
      }),
    ]);

    const child = at(at(tree, 0).children, 0);
    expect(child.status).toBe("completed");
    expect(child.spend.inputTokens).toBe(120);
  });

  it("被硬取消的子运行不会一直显示成等待中", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 1, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(PARENT, "AgentCompleted", 2, {
        child_agent_run_id: CHILD,
        status: "cancelled",
        stop_reason: "cancelled",
        usage: usage(0, 0, 0),
      }),
    ]);

    expect(at(at(tree, 0).children, 0).status).toBe("cancelled");
  });

  it("转述里的 stop_reason 也一并收下", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 1, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(PARENT, "AgentCompleted", 2, {
        child_agent_run_id: CHILD,
        status: "failed",
        stop_reason: "token_budget",
        usage: usage(120_000, 0),
      }),
    ]);

    const child = at(at(tree, 0).children, 0);
    expect(child.status).toBe("failed");
    expect(child.stopReason).toBe("token_budget");
    // AgentCompleted 不带 error：二手知道它失败了，不等于二手知道它为什么失败。
    expect(child.failure).toBeNull();
  });

  it("认不出来的状态仍然落到 unknown，而不是被硬塞成某一种", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 1, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(PARENT, "AgentCompleted", 2, {
        child_agent_run_id: CHILD,
        status: "evaporated",
        usage: usage(0, 0, 0),
      }),
    ]);

    expect(at(at(tree, 0).children, 0).status).toBe("unknown");
  });
});

describe("每个运行自己声明的上限", () => {
  it("RunStarted 带来的是这次运行自己的天花板，不是配置里今天的值", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1, {
        run_kind: "task",
        model_profile: "main",
        tool_names: ["project_read", "delegate_agent"],
        budget: {
          max_steps: 40,
          max_tool_calls: 20,
          max_total_tokens: null,
          max_cost_micro_usd: null,
          deadline: null,
        },
      }),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 3, {
        run_kind: "task",
        model_profile: "sub",
        tool_names: ["web_search"],
        budget: {
          max_steps: 12,
          max_tool_calls: 8,
          max_total_tokens: 120_000,
          max_cost_micro_usd: null,
          deadline: null,
        },
      }),
    ]);

    const parent = at(tree, 0);
    expect(parent.ceiling.maxTotalTokens).toBeNull();
    expect(parent.modelProfile).toBe("main");
    expect(parent.toolCount).toBe(2);

    const child = at(parent.children, 0);
    expect(child.ceiling).toEqual({
      maxSteps: 12,
      maxToolCalls: 8,
      maxTotalTokens: 120_000,
    });
    expect(child.modelProfile).toBe("sub");
    expect(child.toolCount).toBe(1);
  });

  it("没有 RunStarted 在视野里的运行不会凭空得到一个上限", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 1, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
    ]);

    expect(at(at(tree, 0).children, 0).ceiling).toEqual({
      maxSteps: null,
      maxToolCalls: null,
      maxTotalTokens: null,
    });
  });
});

describe("为什么停下来", () => {
  it("RunFailed 的 error 被读出来，行才说得出失败原因", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "RunFailed", 2, {
        stop_reason: "error",
        error: {
          code: "tool_timeout",
          message: "web_search exceeded 30s",
          retryable: true,
        },
        usage: usage(900, 100),
      }),
    ]);

    expect(at(tree, 0).failure).toEqual({
      code: "tool_timeout",
      message: "web_search exceeded 30s",
    });
    expect(at(tree, 0).stopReason).toBe("error");
  });

  it("RunCancelled 是一个终结事件，不是一个没人认识的事件", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "RunCancelled", 2, {
        reason_code: "cancel_requested",
        usage: usage(40, 10),
      }),
    ]);

    expect(at(tree, 0).status).toBe("cancelled");
    expect(at(tree, 0).spend.inputTokens).toBe(40);
  });
});

describe("暂停", () => {
  it("停在审批门上的运行记下它在等什么", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "RunPaused", 2, { reason: "approval" }),
    ]);

    expect(at(tree, 0).pausedFor).toBe("approval");
    expect(at(tree, 0).status).toBe("running");
  });

  it("下一个事件到了就不再是等待", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "RunPaused", 2, { reason: "approval" }),
      event(PARENT, "ToolStarted", 3),
    ]);

    expect(at(tree, 0).pausedFor).toBeNull();
  });

  it("只写过一次 RunPaused 的 id 也算一个运行", () => {
    const tree = buildRunTree([
      event(PARENT, "RunPaused", 1, { reason: "migration" }),
    ]);

    expect(tree.map((node) => node.runId)).toEqual([PARENT]);
  });
});

describe("token 总数与运行时判定预算的算法一致", () => {
  it("cache_write 计入总数，cache_read 不计——它已经在 input 里了", () => {
    const spend = {
      steps: 1,
      toolCalls: 0,
      inputTokens: 1_000,
      outputTokens: 200,
      cacheWriteTokens: 300,
    };

    // 与 domain/runs.py::TokenUsage.total 同一条算式。判 max_total_tokens 的
    // 是含 cache_write 的那个数，面板拿另一套算法去画就会把运行画得比运行时
    // 认为的更远离它的天花板。
    expect(totalTokens(spend)).toBe(1_500);
  });

  it("合计把子运行的 cache_write 也带上", () => {
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 1),
      event(PARENT, "AgentDelegated", 2, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunCompleted", 3, {
        usage: {
          steps: 2,
          tool_calls: 1,
          tokens: {
            input_tokens: 100,
            output_tokens: 50,
            cache_read_tokens: 20,
            cache_write_tokens: 70,
          },
          cost_micro_usd: 0,
        },
      }),
      event(PARENT, "RunCompleted", 4, { usage: usage(300, 100) }),
    ]);

    expect(totalSpend(tree).cacheWriteTokens).toBe(70);
  });
});

describe("位置：被宣告、还没开口的孩子也有一个可以滚过去的地方", () => {
  it("孩子的 firstSequence 是宣告它的那次委派，与服务端 sequence 同源", () => {
    // 服务端 `application/run_tree.py` 的 AgentDelegated 分支正是这么填的。
    // 这条之前两侧相反：前端要等孩子自己写下第一个事件才给它位置，而那正是
    // 这个字段唯一存在意义的那个状态——孩子还没写任何东西。
    const tree = buildRunTree([
      event(PARENT, "RunStarted", 10),
      event(PARENT, "AgentDelegated", 11, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
    ]);

    expect(at(at(tree, 0).children, 0).firstSequence).toBe(11);
  });

  it("孩子自己开口之后，位置仍然是那次委派而不是它的第一个事件", () => {
    const tree = buildRunTree([
      event(PARENT, "AgentDelegated", 11, {
        child_agent_run_id: CHILD,
        profile_name: "analyst",
      }),
      event(CHILD, "RunStarted", 12),
    ]);

    expect(at(at(tree, 0).children, 0).firstSequence).toBe(11);
  });
});

describe("时间：事件一直带着它，这个模块一直没读", () => {
  it("一个运行的起止时间来自它自己的第一条和最后一条事件", () => {
    // `EventEnvelope.timestamp` 是必填字段，从信封被写下那天就在，而这个模块
    // 在此之前只读 `sequence`。所以「这个子代理跑了多久」不是缺数据，是没人问过。
    const roots = buildRunTree([
      event(PARENT, "RunStarted", 1, {}, "work", "2026-08-26T12:00:00Z"),
      event(PARENT, "ModelStarted", 2, {}, "work", "2026-08-26T12:00:07Z"),
      event(PARENT, "RunCompleted", 3, {}, "work", "2026-08-26T12:01:30Z"),
    ]);

    const run = at(roots, 0);
    expect(run.startedAt).toBe("2026-08-26T12:00:00Z");
    expect(run.lastEventAt).toBe("2026-08-26T12:01:30Z");
    expect(runDurationMs(run)).toBe(90_000);
  });

  it("被宣告、还没开口的孩子没有时间，尽管它已经有了位置", () => {
    // 它的位置来自父运行的 `AgentDelegated`（规则二与「位置」那两块钉着这件
    // 事）。时间不跟着来：那条事件是**父运行**的，拿它当孩子的开始时间，等于
    // 把孩子的寿命从它被点名的那一刻算起。
    const roots = buildRunTree([
      event(PARENT, "RunStarted", 1, {}, "work", "2026-08-26T12:00:00Z"),
      event(
        PARENT,
        "AgentDelegated",
        2,
        { child_agent_run_id: CHILD, profile_name: "analyst" },
        "work",
        "2026-08-26T12:00:05Z",
      ),
    ]);

    const child = at(at(roots, 0).children, 0);
    expect(child.firstSequence).toBe(2);
    expect(child.startedAt).toBeNull();
    expect(child.lastEventAt).toBeNull();
    expect(runDurationMs(child)).toBeNull();
  });

  it("只有一条事件的运行没有跨度，而不是零跨度", () => {
    // 0 会被渲染成「0 秒」，读起来是一个什么都没干的运行；实际上是一个还没跑到
    // 第二条事件的运行。
    const roots = buildRunTree([
      event(PARENT, "RunStarted", 1, {}, "work", "2026-08-26T12:00:00Z"),
    ]);

    expect(runDurationMs(at(roots, 0))).toBeNull();
  });

  it("解不出来的时间戳等于没有时间，而不是 NaN", () => {
    // `Date.parse` 对认不出的字符串答 NaN，而 NaN 会一路无声地流到界面上变成
    // 「NaN 秒」。这个页面读不懂的时间戳，就是它没有的事实。
    const roots = buildRunTree([
      event(PARENT, "RunStarted", 1, {}, "work", "not-a-timestamp"),
      event(PARENT, "RunCompleted", 2, {}, "work", "2026-08-26T12:00:10Z"),
    ]);

    expect(runDurationMs(at(roots, 0))).toBeNull();
  });
});
