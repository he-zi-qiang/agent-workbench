import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../../api/types";
import { deriveLifecycle, graphShapeOf, stageOfNode } from "./lifecycle";

describe("which graph a timeline belongs to", () => {
  it("trusts the submission event before any node has run", () => {
    // TaskSubmitted is written with the Task row and carries the resolved
    // version, so the shape is settled from the first event -- a queued v2
    // Task previews its own four stages, not v1's promise of research.
    expect(
      graphShapeOf([envelope("e1", "TaskSubmitted", { graph_version: "v2_general" })]),
    ).toBe("v2");
    expect(
      graphShapeOf([envelope("e1", "TaskSubmitted", { graph_version: "v1" })]),
    ).toBe("v1");
  });

  it("falls back to the node ids when the submission event is out of view", () => {
    expect(graphShapeOf([envelope("e1", "RunStarted", {}, "work")])).toBe("v2");
    expect(graphShapeOf([envelope("e1", "RunStarted", {}, "synthesize")])).toBe("v1");
  });

  it("defaults to v1 so every pre-v2 task renders exactly as before", () => {
    expect(graphShapeOf([])).toBe("v1");
    expect(graphShapeOf([envelope("e1", "TaskClaimed")])).toBe("v1");
  });
});

describe("the lifecycle a reader follows", () => {
  it("declares v2's four stages and none of v1's, from the submission event", () => {
    const lifecycle = deriveLifecycle(
      [
        envelope("e1", "TaskSubmitted", { graph_version: "v2_general" }),
        envelope("e2", "RunStarted", {}, "understand"),
        envelope("e3", "RunStarted", {}, "work"),
      ],
      "running",
    );

    expect(lifecycle.stages.map((stage) => stage.id)).toEqual([
      "understand",
      "work",
      "review",
      "deliver",
    ]);
    // No skipped-forever rows for research it was never going to do.
    expect(lifecycle.stages.map((stage) => stage.title)).toEqual([
      "理解目标",
      "动手做事",
      "检查与修订",
      "确认与产出",
    ]);
    expect(lifecycle.currentTitle).toBe("动手做事");
  });

  it("keeps v1's six stages byte-for-byte", () => {
    const lifecycle = deriveLifecycle(
      [envelope("e1", "TaskSubmitted", { graph_version: "v1" })],
      "running",
    );

    expect(lifecycle.stages.map((stage) => stage.id)).toEqual([
      "understand",
      "plan",
      "research",
      "synthesize",
      "review",
      "deliver",
    ]);
  });

  it("groups both graphs' nodes without a shape in hand", () => {
    // One mapping serves both graphs because their node sets only overlap on
    // the ids they deliberately share, and those land in the same stage.
    expect(stageOfNode("work")).toBe("work");
    expect(stageOfNode("review")).toBe("review");
    expect(stageOfNode("critic")).toBe("review");
    expect(stageOfNode("approval")).toBe("deliver");
    expect(stageOfNode("synthesize")).toBe("synthesize");
    expect(stageOfNode("node_the_future_added")).toBe("node_the_future_added");
  });
});

function envelope(
  eventId: string,
  kind: string,
  payload: Record<string, unknown> = {},
  graphNodeId: string | null = null,
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: eventId,
    stream_id: "stream_1",
    run_id: "run_1",
    event_type: kind,
    durability: "durable",
    timestamp: "2026-08-02T12:00:00Z",
    payload: { kind, ...payload },
    sequence: null,
    task_id: "task_1",
    graph_node_id: graphNodeId,
    parent_event_id: null,
  };
}
