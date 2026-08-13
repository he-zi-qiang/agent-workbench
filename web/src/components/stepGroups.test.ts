import { describe, expect, it } from "vitest";
import type { EventEnvelope, EventPayload } from "../api/types";
import { groupSteps } from "./stepGroups";

/**
 * Folding a run into the steps it took.
 *
 * The property every test here is really guarding is conservation: whatever
 * goes in comes out. A grouping that reads well and quietly loses a permission
 * denial is worse than the raw stream it replaced, because the raw stream at
 * least did not claim to be a summary. So the count assertions are paired with
 * an assertion that the events are all still there.
 */

let nextId = 0;

function event(
  eventType: string,
  payload: Omit<EventPayload, "kind">,
): EventEnvelope {
  nextId += 1;
  return {
    schema_version: 1,
    event_id: `evt_${nextId}`,
    stream_id: "stream_1",
    run_id: "run_1",
    event_type: eventType,
    durability: "durable",
    timestamp: "2026-08-13T04:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence: nextId,
    task_id: "task_1",
    graph_node_id: "research_external",
    parent_event_id: null,
  };
}

/** The five events one successful tool call actually emits. */
function toolCall(
  id: string,
  toolName: string,
  argumentPreview: string,
): EventEnvelope[] {
  return [
    event("ToolProposed", {
      tool_call_id: id,
      tool_name: toolName,
      argument_preview: argumentPreview,
      risk: "external",
    }),
    event("PermissionRequested", { tool_call_id: id, tool_name: toolName }),
    event("PermissionResolved", { tool_call_id: id, effect: "allow" }),
    event("ToolStarted", { tool_call_id: id, tool_name: toolName }),
    event("ToolCompleted", { tool_call_id: id, output_bytes: 120 }),
  ];
}

/** A model turn that only decided to call tools, as the runtime emits it. */
function toolUseTurn(id: string, calls: string[]): EventEnvelope[] {
  return [
    event("ModelStarted", { model_call_id: id, model_id: "deepseek-chat" }),
    event("ModelCompleted", {
      model_call_id: id,
      finish_reason: "tool_use",
      text: "",
      tool_call_ids: calls,
    }),
  ];
}

function totalEvents(groups: ReturnType<typeof groupSteps>): number {
  return groups.reduce((count, group) => count + group.events.length, 0);
}

describe("groupSteps", () => {
  it("folds one tool call's five events into a single step", () => {
    const events = toolCall("tc_1", "mcp_web_fetch_page", '{"url":"https://a.example/x"}');

    const groups = groupSteps(events);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.title).toBe("读取网页");
    expect(groups[0]?.subject).toBe("https://a.example/x");
    expect(groups[0]?.outcome).toBe("ok");
    // Conservation: the five raw events are still reachable underneath.
    expect(groups[0]?.events).toHaveLength(5);
  });

  it("gives the run the step count a reader would say, not the event count", () => {
    /**
     * The regression this module exists for. A stage that read four pages
     * reported "14 步" of lifecycle vocabulary; a reader counting what the
     * agent did would say four.
     */
    const events = [
      ...toolUseTurn("mc_1", ["tc_1", "tc_2"]),
      ...toolCall("tc_1", "mcp_web_fetch_page", '{"url":"https://a.example/1"}'),
      ...toolCall("tc_2", "mcp_web_fetch_page", '{"url":"https://a.example/2"}'),
      ...toolUseTurn("mc_2", ["tc_3"]),
      ...toolCall("tc_3", "mcp_web_fetch_page", '{"url":"https://a.example/3"}'),
      ...toolUseTurn("mc_3", ["tc_4"]),
      ...toolCall("tc_4", "mcp_word_render_document", '{"name":"report.docx"}'),
    ];

    const groups = groupSteps(events);

    expect(events).toHaveLength(26);
    expect(groups).toHaveLength(4);
    expect(groups.map((group) => group.title)).toEqual([
      "读取网页",
      "读取网页",
      "读取网页",
      "生成 Word 文档",
    ]);
    // Nothing was dropped on the way to that number.
    expect(totalEvents(groups)).toBe(26);
  });

  it("keeps a model turn that produced text as a step of its own", () => {
    /**
     * The control for the fold above. A turn that said something is the agent
     * speaking and has to stay visible; only the turns that merely named tools
     * are bookkeeping.
     */
    const events = [
      event("ModelStarted", { model_call_id: "mc_1", model_id: "deepseek-chat" }),
      event("ModelCompleted", {
        model_call_id: "mc_1",
        finish_reason: "stop",
        text: "我先确认调研范围。",
        tool_call_ids: [],
      }),
    ];

    const groups = groupSteps(events);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.title).toBe("模型作答");
    expect(groups[0]?.events).toHaveLength(2);
    // A turn that came back is done. Left at the "running" it opens with, a
    // settled Task showed a column of 进行中 beside steps that had plainly
    // ended -- which is what it looked like on the first run of this page.
    expect(groups[0]?.outcome).toBe("ok");
  });

  it("marks a model turn still in flight as running", () => {
    const events = [
      event("ModelStarted", { model_call_id: "mc_1", model_id: "deepseek-chat" }),
    ];

    expect(groupSteps(events)[0]?.outcome).toBe("running");
  });

  it("files a text-less turn under the first call it named", () => {
    const events = [
      ...toolUseTurn("mc_1", ["tc_1"]),
      ...toolCall("tc_1", "workspace_write", '{"name":"draft.md"}'),
    ];

    const groups = groupSteps(events);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.title).toBe("写入工作区");
    // The turn's two events lead the group: it caused the call, so it reads
    // before it.
    expect(groups[0]?.events.map((item) => item.event_type)).toEqual([
      "ModelStarted",
      "ModelCompleted",
      "ToolProposed",
      "PermissionRequested",
      "PermissionResolved",
      "ToolStarted",
      "ToolCompleted",
    ]);
  });

  it("keeps a turn whose tool call is not on this page rather than losing it", () => {
    /**
     * A truncated timeline, or a run still in flight. Folding into a group that
     * is not here would delete the turn, which is the one thing this module
     * must never do.
     */
    const events = toolUseTurn("mc_1", ["tc_never_arrived"]);

    const groups = groupSteps(events);

    expect(groups).toHaveLength(1);
    expect(totalEvents(groups)).toBe(2);
  });

  it("reports a denial as the denial rather than the failure it caused", () => {
    /**
     * Both events arrive: policy says no, and the call then fails *because* it
     * said no. Reporting "失败" would name the consequence and hide the cause,
     * sending a reader to look for a broken tool instead of a missing scope.
     */
    const events = [
      event("ToolProposed", {
        tool_call_id: "tc_1",
        tool_name: "external_search",
        argument_preview: '{"query":"deepseek 最新模型"}',
        risk: "external",
      }),
      event("PermissionResolved", {
        tool_call_id: "tc_1",
        effect: "deny",
        reason_code: "outside_submitted_envelope",
      }),
      event("ToolFailed", {
        tool_call_id: "tc_1",
        error: { code: "policy_denied", message: "denied" },
      }),
    ];

    const groups = groupSteps(events);

    expect(groups[0]?.outcome).toBe("denied");
    expect(groups[0]?.subject).toBe("deepseek 最新模型");
  });

  it("marks a call that has not finished as running", () => {
    const events = [
      event("ToolProposed", {
        tool_call_id: "tc_1",
        tool_name: "mcp_web_fetch_page",
        argument_preview: '{"url":"https://a.example/x"}',
        risk: "external",
      }),
      event("ToolStarted", { tool_call_id: "tc_1", tool_name: "mcp_web_fetch_page" }),
    ];

    expect(groupSteps(events)[0]?.outcome).toBe("running");
  });

  it("shows a tool it has no phrase for under its real name", () => {
    /**
     * The alternative -- a generic "调用工具" -- would make two different tools
     * look like the same step, and would hide a newly added tool behind a word
     * that describes nothing.
     */
    const groups = groupSteps(toolCall("tc_1", "some_new_tool", "{}"));

    expect(groups[0]?.title).toBe("some_new_tool");
    expect(groups[0]?.subject).toBeNull();
  });

  it("leaves events it has no opinion about as their own steps", () => {
    const events = [
      event("RunStarted", { run_kind: "task", tool_names: [] }),
      ...toolCall("tc_1", "workspace_list", "{}"),
      event("RunCompleted", { stop_reason: "stop" }),
    ];

    const groups = groupSteps(events);

    expect(groups.map((group) => group.events.length)).toEqual([1, 5, 1]);
    expect(totalEvents(groups)).toBe(7);
  });

  it("keeps groups in the order their first event arrived", () => {
    const events = [
      ...toolCall("tc_1", "workspace_list", "{}"),
      ...toolCall("tc_2", "workspace_write", '{"name":"a.md"}'),
      // A late event for the first call must not move it to the end.
      event("ToolProgress", { tool_call_id: "tc_1" }),
    ];

    const groups = groupSteps(events);

    expect(groups.map((group) => group.key)).toEqual(["tool:tc_1", "tool:tc_2"]);
    expect(groups[0]?.events).toHaveLength(6);
  });

  it("truncates a subject too long for one line without dropping it", () => {
    const query = "a".repeat(200);
    const groups = groupSteps(
      toolCall("tc_1", "web_search", JSON.stringify({ query })),
    );

    const subject = groups[0]?.subject ?? "";
    expect(subject.length).toBeLessThanOrEqual(56);
    expect(subject.endsWith("…")).toBe(true);
  });

  it("returns nothing for nothing", () => {
    expect(groupSteps([])).toEqual([]);
  });
});
