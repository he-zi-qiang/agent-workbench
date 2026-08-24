import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../api/types";
import type { StepGroup } from "./stepGroups";
import { presentActivity } from "./activityPresentation";

function event(
  kind: string,
  payload: Record<string, unknown>,
  index: number,
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `evt_${String(index)}`,
    stream_id: "ses_1",
    run_id: "run_1",
    event_type: kind,
    durability: "durable",
    timestamp: `2026-08-23T12:00:0${String(index)}Z`,
    payload: { kind, ...payload },
    sequence: index,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
}

function group(events: EventEnvelope[]): StepGroup {
  return {
    key: "tool:call_1",
    title: "运行代码",
    subject: "check.py",
    outcome: "ok",
    gate: null,
    events,
  };
}

describe("activity presentation", () => {
  it("keeps a thought beside the command it caused", () => {
    const view = presentActivity(
      group([
        event(
          "ModelCompleted",
          { thinking_preview: "先验证文件，再决定是否修改。" },
          1,
        ),
        event(
          "ToolProposed",
          {
            tool_name: "sandbox_run",
            argument_preview: JSON.stringify({
              inputs: ["check.py"],
              script: "python check.py\nprint('done')",
            }),
          },
          2,
        ),
        event("ToolCompleted", { output_preview: "all checks passed" }, 3),
      ]),
    );

    expect(view.reasoning).toBe("先验证文件，再决定是否修改。");
    expect(view.command).toEqual({
      summary: "python check.py",
      text: "python check.py\nprint('done')",
      output: "all checks passed",
    });
  });

  it("does not invent a command when the bounded JSON is incomplete", () => {
    const view = presentActivity(
      group([
        event(
          "ToolProposed",
          {
            tool_name: "sandbox_run",
            argument_preview: '{"script":"python check.py',
          },
          1,
        ),
      ]),
    );

    expect(view.command).toBeNull();
  });

  it("does not present arbitrary tool arguments as shell commands", () => {
    const view = presentActivity(
      group([
        event(
          "ToolProposed",
          {
            tool_name: "web_search",
            argument_preview: JSON.stringify({ command: "not a command", query: "weather" }),
          },
          1,
        ),
      ]),
    );

    expect(view.command).toBeNull();
  });
});
