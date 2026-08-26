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

import type { EventEnvelope } from "../../api/types";
import {
  buildRunTree,
  flattenRuns,
  totalSpend,
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
