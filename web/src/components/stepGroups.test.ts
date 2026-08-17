import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../api/types";
import { groupSteps, summariseGroups, type StepGroup } from "./stepGroups";

function group(title: string, key: string): StepGroup {
  return { key, title, subject: null, outcome: "ok", events: [] };
}

function groups(...titles: string[]): StepGroup[] {
  return titles.map((title, index) => group(title, `k${String(index)}`));
}

/**
 * `groupSteps` had no tests at all, and a whole feature now rests on one of its
 * behaviours: a model turn that only called tools is filed *ahead of* the first
 * call it named, which is what puts a turn's reasoning next to the action it
 * caused rather than in a list of its own. That merge has three implicit
 * conditions, and each fails differently, so each is pinned here.
 */

let nextEvent = 0;

function event(
  type: string,
  payload: Record<string, unknown>,
): EventEnvelope {
  nextEvent += 1;
  return {
    schema_version: 1,
    event_id: `evt_${String(nextEvent)}`,
    stream_id: "stream_1",
    run_id: "run_1",
    event_type: type,
    durability: "durable",
    timestamp: "2026-08-17T12:00:00Z",
    payload: { kind: type, ...payload },
    sequence: nextEvent,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
}

describe("groupSteps", () => {
  it("files a text-less model turn ahead of the call it named", () => {
    // The condition the Code transcript depends on. A tool-calling turn comes
    // back with an empty `text` (DeepSeek sends `content: ''`), so the turn is
    // not a step of its own -- it is how the next line came to happen, and it
    // is merged in front of that line. The reasoning it carries therefore
    // arrives already positioned.
    const result = groupSteps([
      event("ModelStarted", { model_call_id: "mc_1" }),
      event("ModelCompleted", {
        model_call_id: "mc_1",
        text: "",
        tool_call_ids: ["call_1"],
        thinking_preview: "先看看工作区里有什么",
      }),
      event("ToolProposed", {
        tool_call_id: "call_1",
        tool_name: "workspace_list",
      }),
      event("ToolCompleted", { tool_call_id: "call_1" }),
    ]);

    expect(result).toHaveLength(1);
    const only = result[0];
    expect(only?.key).toBe("tool:call_1");
    expect(only?.title).toBe("查看工作区");
    // Prepended, not appended: the thinking has to read as coming *before* the
    // action, and the array order is the only thing that says so.
    expect(only?.events.map((e) => e.event_type)).toEqual([
      "ModelStarted",
      "ModelCompleted",
      "ToolProposed",
      "ToolCompleted",
    ]);
    // And no `model:` group survives to hold a second copy of the same thought.
    expect(result.some((g) => g.key.startsWith("model:"))).toBe(false);
  });

  it("leaves a model turn that named no reachable call in its own row", () => {
    // The truncated-timeline case. A turn whose call is not on this page keeps
    // its own row rather than vanishing into a group that is not here -- so a
    // thought never disappears just because the event window cut its action off.
    const result = groupSteps([
      event("ModelCompleted", {
        model_call_id: "mc_1",
        text: "",
        tool_call_ids: ["call_missing"],
        thinking_preview: "这段推理的动作不在本页",
      }),
    ]);

    expect(result).toHaveLength(1);
    expect(result[0]?.key).toBe("model:mc_1");
  });

  it("keeps a turn that produced text as a step of its own", () => {
    // The answering turn. It is not folded anywhere, because the thing it did
    // *is* saying something -- and the report that follows is that text.
    const result = groupSteps([
      event("ModelCompleted", {
        model_call_id: "mc_1",
        text: "已完成。",
        thinking_preview: "写完了，交代一下",
      }),
    ]);

    expect(result).toHaveLength(1);
    expect(result[0]?.key).toBe("model:mc_1");
    expect(result[0]?.title).toBe("模型作答");
  });

  it("files a turn that named two calls onto the first of them", () => {
    // Two rows either way; putting the turn on the earlier one keeps it ahead
    // of the work it caused. It also means one model call is never rendered as
    // two thoughts.
    const result = groupSteps([
      event("ModelCompleted", {
        model_call_id: "mc_1",
        text: "",
        tool_call_ids: ["call_a", "call_b"],
        thinking_preview: "两件事一起做",
      }),
      event("ToolProposed", { tool_call_id: "call_a", tool_name: "workspace_read" }),
      event("ToolProposed", { tool_call_id: "call_b", tool_name: "workspace_write" }),
    ]);

    expect(result.map((g) => g.key)).toEqual(["tool:call_a", "tool:call_b"]);
    const carrying = result.filter((g) =>
      g.events.some((e) => e.event_type === "ModelCompleted"),
    );
    expect(carrying).toHaveLength(1);
    expect(carrying[0]?.key).toBe("tool:call_a");
  });
});

describe("summariseGroups", () => {
  it("names what a stage did instead of counting it", () => {
    // The line this replaces read "16 步", which is the whole account of a
    // finished stage until somebody clicks it open.
    expect(
      summariseGroups(
        groups(
          ...Array<string>(12).fill("读取网页"),
          ...Array<string>(3).fill("搜索网络"),
          "模型作答",
        ),
      ),
    ).toBe("读取网页 ×12 · 搜索网络 ×3 · 模型作答");
  });

  it("drops the ×1 that says nothing the title does not", () => {
    expect(summariseGroups(groups("写入工作区"))).toBe("写入工作区");
  });

  it("stops at three kinds and says how many it stopped at", () => {
    // Past three this is no longer a line; it is the list it stands in for.
    expect(
      summariseGroups(groups("甲", "甲", "乙", "丙", "丁", "戊")),
    ).toBe("甲 ×2 · 乙 · 丙 · 等 2 项");
  });

  it("orders by count, and ties by which appeared first", () => {
    // Stable across polls: a digest that reshuffled when two kinds drew level
    // would look like the stage had changed when only the sort had.
    expect(summariseGroups(groups("乙", "甲", "甲", "乙"))).toBe("乙 ×2 · 甲 ×2");
  });

  it("says nothing about a stage that has done nothing", () => {
    expect(summariseGroups([])).toBe("");
  });
});
