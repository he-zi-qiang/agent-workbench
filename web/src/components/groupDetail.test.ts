import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import { describeGroup } from "./groupDetail";
import { groupSteps } from "./stepGroups";

/**
 * 一次工具调用是五条事件；这里钉的是它们并成**一件事**之后不能走样的地方：
 * 三段正文按发生的顺序排、簿记标签不进来、失败那一句说的是原因不是后果。
 */

let sequence = 0;

function event(eventType: string, payload: Record<string, unknown> = {}): EventEnvelope {
  sequence += 1;
  return {
    schema_version: 1,
    event_id: `evt_${String(sequence)}`,
    stream_id: "stream_1",
    run_id: "run_1",
    event_type: eventType,
    durability: "durable",
    timestamp: "2026-08-24T11:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
}

/** 一次 `project_read`，和服务端真的发出来的一样（实测 ses_a1a4504f）。 */
function read(outcome: "ok" | "failed" | "denied") {
  const events = [
    event("ModelCompleted", {
      model_call_id: "mc_1",
      thinking_preview: "先读一下这个文件。",
      text: "",
      tool_call_ids: ["call_1"],
    }),
    event("ToolProposed", {
      tool_call_id: "call_1",
      tool_name: "project_read",
      risk: "read",
      argument_bytes: 26,
      argument_sha256: "abcdef0123456789abcdef0123456789",
      argument_preview: '{"path":"docs/hello.html"}',
    }),
    event("PermissionResolved", {
      tool_call_id: "call_1",
      effect: outcome === "denied" ? "deny" : "allow",
      reason_code: outcome === "denied" ? "policy_denied" : "within_submitted_envelope",
    }),
  ];
  if (outcome === "denied") {
    events.push(
      event("ToolFailed", {
        tool_call_id: "call_1",
        error: {
          code: "policy_denied",
          message: "project_read was not permitted: nobody answered within its 120s bound",
          retryable: false,
        },
      }),
    );
  } else if (outcome === "failed") {
    events.push(
      event("ToolStarted", { tool_call_id: "call_1", tool_name: "project_read" }),
      event("ToolFailed", {
        tool_call_id: "call_1",
        error: { code: "not_found", message: "no such file: docs/hello.html", retryable: false },
      }),
    );
  } else {
    events.push(
      event("ToolStarted", { tool_call_id: "call_1", tool_name: "project_read" }),
      event("ToolCompleted", {
        tool_call_id: "call_1",
        duration_ms: 12,
        output_bytes: 176,
        output_preview: "<!DOCTYPE html>\n<html>\n  <h1>Hello</h1>\n</html>\n",
      }),
    );
  }
  const [group] = groupSteps(events);
  if (group === undefined) throw new Error("groupSteps produced nothing");
  return group;
}

describe("describeGroup", () => {
  it("三段正文按发生的顺序：思考、参数、返回", () => {
    const detail = describeGroup(read("ok"));

    expect(detail.bodies.map((body) => body.label)).toEqual([
      "思考摘要",
      "调用参数",
      "工具返回",
    ]);
    // 参数解析成了值，给 JsonView 按形状画；返回是文本，照文本画。
    expect(detail.bodies[1]?.value).toEqual({ path: "docs/hello.html" });
    expect(detail.bodies[2]?.value).toBeUndefined();
    expect(detail.bodies[2]?.text).toContain("<h1>Hello</h1>");
    expect(detail.failure).toBeNull();
  });

  it("事实按标签去重，簿记那几个不进来", () => {
    const detail = describeGroup(read("ok"));
    const labels = detail.facts.map((fact) => fact.label);

    // 「工具」在提出和开始两条上各写了一遍，这里一次。
    expect(labels.filter((label) => label === "工具")).toHaveLength(1);
    expect(labels).toContain("耗时");
    expect(labels).toContain("输出大小");
    // sha 摘要、调用 ID、参数大小在原始事件里仍然有，在「它做了什么」里只是噪声。
    expect(labels).not.toContain("参数摘要");
    expect(labels).not.toContain("参数大小");
    expect(labels).not.toContain("调用 ID");
    // 提出这次调用的那次模型调用是上下文，不是这一步做的事：它的 token、档位
    // 和整段提示词不进工具组，只有思考摘要进。
    expect(labels).not.toContain("模型");
    expect(labels).not.toContain("输入 token");
    expect(labels).not.toContain("结束原因");
  });

  it("模型组（模型作答）照旧带自己的事实和正文", () => {
    const [group] = groupSteps([
      event("ModelStarted", { model_call_id: "mc_9", model_id: "deepseek-v4-flash" }),
      event("ModelCompleted", {
        model_call_id: "mc_9",
        finish_reason: "stop",
        text: "已经改好了。",
        usage: { input_tokens: 10, output_tokens: 4 },
      }),
    ]);
    if (group === undefined) throw new Error("groupSteps produced nothing");
    const detail = describeGroup(group);
    const labels = detail.facts.map((fact) => fact.label);

    expect(labels).toContain("模型");
    expect(labels).toContain("输入 token");
    expect(detail.bodies.map((body) => body.label)).toEqual(["模型输出"]);
  });

  it("失败那一句用词表叫出错误码，再接服务端自己的话", () => {
    const detail = describeGroup(read("failed"));

    expect(detail.failure).toBe("需要的资源不存在：no such file: docs/hello.html");
    expect(detail.bodies.map((body) => body.label)).toContain("错误信息");
  });

  it("被拒的调用说的是拒绝，不是它引起的那次失败", () => {
    const detail = describeGroup(read("denied"));

    // 失败往往只是拒绝的后果，读者要的是原因。
    expect(detail.failure).toContain("被拒绝");
    expect(detail.failure).toContain("nobody answered within its 120s bound");
  });

  it("bodies: false 时不带正文，事实与失败照旧", () => {
    const detail = describeGroup(read("failed"), { bodies: false });

    expect(detail.bodies).toEqual([]);
    expect(detail.facts.length).toBeGreaterThan(0);
    expect(detail.failure).not.toBeNull();
  });
});
