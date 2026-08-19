import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../api/types";
import { groupSteps, summariseGroups, type StepGroup } from "./stepGroups";

function group(title: string, key: string): StepGroup {
  return { key, title, subject: null, outcome: "ok", gate: null, events: [] };
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

describe("groupSteps 的授权四件套", () => {
  const states = (
    gate: ReturnType<typeof groupSteps>[number]["gate"] | undefined,
  ) => gate?.map((step) => [step.label, step.state]);

  it("四颗珠子只由到达的事件点亮", () => {
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_1",
        tool_name: "knowledge_search",
      }),
      event("PermissionResolved", { tool_call_id: "call_1", effect: "allow" }),
      event("ToolStarted", { tool_call_id: "call_1", tool_name: "knowledge_search" }),
      event("ToolCompleted", { tool_call_id: "call_1" }),
    ]);

    expect(states(result[0]?.gate)).toEqual([
      ["提议", "done"],
      ["授权", "done"],
      ["开始", "done"],
      ["完成", "done"],
    ]);
  });

  it("被策略拒绝的调用停在第二颗，后两颗仍是未到", () => {
    // 设计稿画的就是这一条：被拒之后没有 ToolStarted，也就没有「开始」。
    // 把后两颗画成灰色的「done」会让读者以为它跑过了但没成功。
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_1",
        tool_name: "mcp_web_fetch_page",
      }),
      event("PermissionResolved", {
        tool_call_id: "call_1",
        effect: "deny",
        reason_code: "external_fetch_blocked",
      }),
    ]);

    expect(states(result[0]?.gate)).toEqual([
      ["提议", "done"],
      ["被拒", "denied"],
      ["开始", "pending"],
      ["完成", "pending"],
    ]);
  });

  it("人否掉策略已经放行的调用，第二颗照样是被拒", () => {
    // PermissionResolved 说 allow，ToolApprovalDecided 说 deny——两者可以不
    // 一致，而读者要看到的是最终拦下它的那一个。
    const result = groupSteps([
      event("ToolProposed", { tool_call_id: "call_1", tool_name: "sandbox_run" }),
      event("PermissionResolved", { tool_call_id: "call_1", effect: "allow" }),
      event("ToolApprovalDecided", {
        tool_call_id: "call_1",
        approval_id: "apr_1",
        decision: "deny",
        decided_by: "human",
      }),
    ]);

    expect(states(result[0]?.gate)?.[1]).toEqual(["被拒", "denied"]);
  });

  it("改写参数的第二轮 allow 不能抹掉第一轮的 deny", () => {
    // 策略网关会为每一轮改写各发一条 PermissionResolved。若按最后一条覆盖，
    // 一次改写就能把调用带过它自己的拒绝。
    const result = groupSteps([
      event("ToolProposed", { tool_call_id: "call_1", tool_name: "sandbox_run" }),
      event("PermissionResolved", { tool_call_id: "call_1", effect: "deny" }),
      event("PermissionResolved", { tool_call_id: "call_1", effect: "allow" }),
    ]);

    expect(states(result[0]?.gate)?.[1]).toEqual(["被拒", "denied"]);
  });

  it("没有裁决就停住的调用，第二颗留在未到", () => {
    // 审批超时：没人回答。这既不是允许也不是拒绝，画成任何一个都是替
    // 那个没出现的人做决定。
    const result = groupSteps([
      event("ToolProposed", { tool_call_id: "call_1", tool_name: "sandbox_run" }),
      event("PermissionRequested", { tool_call_id: "call_1", approval_id: "apr_1" }),
    ]);

    expect(states(result[0]?.gate)).toEqual([
      ["提议", "done"],
      ["授权", "pending"],
      ["开始", "pending"],
      ["完成", "pending"],
    ]);
  });

  it("跑起来之后失败的调用，最后一颗是失败而不是未到", () => {
    const result = groupSteps([
      event("ToolProposed", { tool_call_id: "call_1", tool_name: "sandbox_run" }),
      event("PermissionResolved", { tool_call_id: "call_1", effect: "allow" }),
      event("ToolStarted", { tool_call_id: "call_1", tool_name: "sandbox_run" }),
      event("ToolFailed", { tool_call_id: "call_1", error: { code: "timeout" } }),
    ]);

    expect(states(result[0]?.gate)).toEqual([
      ["提议", "done"],
      ["授权", "done"],
      ["开始", "done"],
      ["完成", "failed"],
    ]);
  });

  it("模型作答没有要过的门，因此没有珠串", () => {
    const result = groupSteps([
      event("ModelStarted", { model_call_id: "mc_1" }),
      event("ModelCompleted", { model_call_id: "mc_1", text: "写完了" }),
    ]);

    expect(result[0]?.gate).toBeNull();
  });
});

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

describe("一行步骤说得出它对哪个文件动的手", () => {
  // 稿子上的步骤行是三段：动词 · 对象 · 结果。实际渲染出来长期只有动词——
  // 「写入工作区」后面什么都没有，读的人没法知道它写了哪个文件。原因不在
  // 渲染层，CodeTurn 一直画着 subject；是 subject 解析不出来。两条真实事件
  // 各踩了一个不同的坑，所以两条都钉在这里。

  it("写入的文件名来自结果，因为参数预览里放不下", () => {
    // 真实载荷：workspace_write 把整个文件内容当第一个字段发出去，
    // argument_preview 截断在一百多字符处，于是 path 永远在切口之外。
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_1",
        tool_name: "workspace_write",
        argument_preview: '{"content":"<!DOCTYPE html>\\n<html lang=\\"zh-CN\\">',
      }),
      event("ToolCompleted", {
        tool_call_id: "call_1",
        workspace_writes: ["snake.html"],
      }),
    ]);

    expect(result[0]?.title).toBe("写入工作区");
    expect(result[0]?.subject).toBe("snake.html");
  });

  it("运行代码的对象来自 inputs 数组，不是字符串字段", () => {
    // sandbox_run 的参数是 {"inputs": ["snake.html"], "script": "…"}，
    // 而 SUBJECT_KEYS 只认字符串值，数组会被整个走过去。
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_2",
        tool_name: "sandbox_run",
        argument_preview: '{"inputs":["snake.html"],"script":"import re"}',
      }),
      event("ToolCompleted", { tool_call_id: "call_2" }),
    ]);

    expect(result[0]?.title).toBe("运行代码");
    expect(result[0]?.subject).toBe("snake.html");
  });

  it("参数里说得出对象时，结果不覆盖它", () => {
    // 参数说的是这次调用**要**对谁动手，即使后来写到了别处，那句话依然是
    // 这一步的意图；结果只在参数说不出话时补位。
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_3",
        tool_name: "workspace_read",
        argument_preview: '{"path":"notes.md"}',
      }),
      event("ToolCompleted", {
        tool_call_id: "call_3",
        workspace_writes: ["something-else.md"],
      }),
    ]);

    expect(result[0]?.subject).toBe("notes.md");
  });

  it("真的没有对象就还是没有，不编一个出来", () => {
    // workspace_list 的参数就是 {}。空着是对的——比填一个「工作区」强，
    // 那个词不增加任何信息，只是让一行看起来填满了。
    const result = groupSteps([
      event("ToolProposed", {
        tool_call_id: "call_4",
        tool_name: "workspace_list",
        argument_preview: "{}",
      }),
      event("ToolCompleted", { tool_call_id: "call_4", workspace_writes: [] }),
    ]);

    expect(result[0]?.title).toBe("查看工作区");
    expect(result[0]?.subject).toBeNull();
  });
});
