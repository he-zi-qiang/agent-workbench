/**
 * Attributing a timeline row to the sub-agent that produced it.
 *
 * The case worth being careful about is the page that does *not* contain the
 * delegation. A child's events carry its run id and nothing about who started
 * it, so there is no way to recover the relationship from them -- and guessing
 * would put another agent's name on a run it never touched, which is worse than
 * leaving the row as it was.
 */

import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../../api/types";
import { readDelegations, titleWithDelegation } from "./delegations";

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
    timestamp: "2026-08-26T12:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence,
    task_id: "task_1",
    graph_node_id: "research_internal",
    parent_event_id: null,
  };
}

function delegation(name: unknown = "analyst"): EventEnvelope {
  return event(PARENT, "AgentDelegated", 2, {
    child_agent_run_id: CHILD,
    profile_name: name,
  });
}

describe("readDelegations", () => {
  it("names the sub-agent each delegated run was started as", () => {
    const facts = readDelegations([
      event(PARENT, "RunStarted", 1),
      delegation("researcher"),
    ]);

    expect(facts.get(CHILD)).toEqual({
      definitionName: "researcher",
      parentRunId: PARENT,
    });
  });

  it("says nothing about a run whose delegation is not on this page", () => {
    const facts = readDelegations([event(CHILD, "RunStarted", 30)]);

    expect(facts.has(CHILD)).toBe(false);
  });

  it("ignores a delegation whose payload names no child", () => {
    const facts = readDelegations([
      event(PARENT, "AgentDelegated", 2, { profile_name: "analyst" }),
    ]);

    expect(facts.size).toBe(0);
  });

  it("reads several delegations from one fan-out", () => {
    const events = [event(PARENT, "RunStarted", 1)];
    ["run_a", "run_b", "run_c"].forEach((runId, index) => {
      events.push(
        event(PARENT, "AgentDelegated", 2 + index, {
          child_agent_run_id: runId,
          profile_name: "analyst",
        }),
      );
    });

    expect([...readDelegations(events).keys()]).toEqual([
      "run_a",
      "run_b",
      "run_c",
    ]);
  });
});

describe("titleWithDelegation", () => {
  it("leaves the parent's own rows alone", () => {
    const delegations = readDelegations([delegation()]);

    expect(
      titleWithDelegation("工具调用失败", event(PARENT, "ToolFailed", 3), delegations),
    ).toBe("工具调用失败");
  });

  it("attributes a delegated row to its sub-agent", () => {
    const delegations = readDelegations([delegation()]);

    expect(
      titleWithDelegation("工具调用失败", event(CHILD, "ToolFailed", 4), delegations),
    ).toBe("子代理 analyst：工具调用失败");
  });

  it("keeps the base title findable inside the prefixed one", () => {
    // A reader scanning for a failure has to find it whichever run produced it,
    // which is why the prefix is a prefix rather than a replacement.
    const delegations = readDelegations([delegation()]);
    const titled = titleWithDelegation(
      "工具调用失败",
      event(CHILD, "ToolFailed", 4),
      delegations,
    );

    expect(titled.endsWith("工具调用失败")).toBe(true);
  });

  it("still marks a delegated row whose delegation named no sub-agent", () => {
    // Its events are somebody else's work either way, and rendering them as
    // the parent's would be the one wrong answer available.
    const delegations = readDelegations([delegation(42)]);

    expect(
      titleWithDelegation("运行已开始", event(CHILD, "RunStarted", 3), delegations),
    ).toBe("子代理：运行已开始");
  });
});
