import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../../api/types";
import type { ChatActivity, ChatActivityState } from "./model";
import { deriveTurnStages, isTurnMetaActivity } from "./turnStages";

/**
 * Grouping a turn's events into the three things a turn does.
 *
 * A Chat turn carries no `graph_node_id` -- every event on it has null -- so
 * these stages come from what the event kinds mean. That is the whole reason
 * this exists separately from Work's node-based lifecycle.
 */
describe("deriveTurnStages", () => {
  it("groups a retrieved turn into 检索资料, 生成回答 and 核对与发布", () => {
    const stages = deriveTurnStages(
      [
        activity("ContextBuilt", "complete"),
        activity("ModelStarted", "running"),
        activity("ModelCompleted", "complete"),
        activity("AnswerCommitted", "complete"),
      ],
      "committed",
    );

    expect(stages.map((stage) => [stage.title, stage.events.length])).toEqual([
      ["检索资料", 1],
      ["生成回答", 2],
      ["核对与发布", 1],
    ]);
    expect(stages.every((stage) => stage.state === "done")).toBe(true);
  });

  it("counts a rejected retrieval as retrieval having happened", () => {
    // The routed shape can search for a minute and then answer without the
    // result. Showing that stage as 未执行 would report the opposite of what
    // the turn did.
    const stages = deriveTurnStages(
      [
        activity("RetrievalRejected", "info"),
        activity("ModelCompleted", "complete"),
        activity("UngroundedAnswerCommitted", "complete"),
      ],
      "committed",
    );

    expect(stages[0]?.state).toBe("done");
    expect(stages[0]?.events).toHaveLength(1);
    expect(stages[0]?.note).not.toBe("未执行");
  });

  it("marks retrieval skipped on a finished direct turn, not pending", () => {
    // The no-knowledge-base path never retrieves. Leaving that stage "等待中"
    // would show unfinished work on a turn that answered correctly.
    const stages = deriveTurnStages(
      [
        activity("ModelStarted", "running"),
        activity("ModelCompleted", "complete"),
        activity("AnswerCommitted", "complete"),
      ],
      "committed",
    );

    expect(stages[0]?.state).toBe("skipped");
    expect(stages[0]?.note).toBe("未执行");
  });

  it("keeps the stage that is moving active while the turn runs", () => {
    const stages = deriveTurnStages(
      [activity("ContextBuilt", "complete"), activity("ModelStarted", "running")],
      "running",
    );

    expect(stages[0]?.state).toBe("done");
    expect(stages[1]?.state).toBe("active");
    expect(stages[1]?.note).toBe("进行中");
    // Not yet reached, and the turn has not finished, so it is still to come.
    expect(stages[2]?.state).toBe("pending");
  });

  it("shows a withheld answer as blocked rather than done", () => {
    const stages = deriveTurnStages(
      [
        activity("ModelCompleted", "complete"),
        activity("AnswerWithheld", "waiting"),
      ],
      "withheld",
    );

    expect(stages[2]?.state).toBe("waiting");
    expect(stages[2]?.note).toBe("已阻止");
  });

  it("marks a stage failed when any of its steps failed", () => {
    const stages = deriveTurnStages(
      [
        activity("ToolProposed", "running"),
        activity("PermissionResolved", "failed"),
      ],
      "failed",
    );

    expect(stages[0]?.state).toBe("failed");
  });

  it("keeps run bookkeeping out of the stages", () => {
    const activities = [
      activity("RunStarted", "running"),
      activity("ContextBuilt", "complete"),
      activity("RunCompleted", "complete"),
    ];
    const stages = deriveTurnStages(activities, "committed");

    expect(stages.flatMap((stage) => stage.events)).toHaveLength(1);
    expect(activities.filter(isTurnMetaActivity).map((one) => one.kind)).toEqual([
      "RunStarted",
      "RunCompleted",
    ]);
  });

  it("shows an unrecognised event instead of dropping it", () => {
    // A server that grows an event type should not make work invisible here.
    const stages = deriveTurnStages(
      [activity("ContextBuilt", "complete"), activity("SomethingNew", "info")],
      "committed",
    );

    expect(stages[0]?.events.map((event) => event.event_type)).toEqual([
      "ContextBuilt",
      "SomethingNew",
    ]);
  });

  it("times a finished stage by its last step", () => {
    const stages = deriveTurnStages(
      [
        activity("ModelStarted", "running", "2026-08-02T12:00:00Z"),
        activity("ModelCompleted", "complete", "2026-08-02T12:00:30Z"),
      ],
      "committed",
    );

    expect(stages[1]?.note).toBe(formatted("2026-08-02T12:00:30Z"));
  });
});

function formatted(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

let counter = 0;

function activity(
  kind: string,
  state: ChatActivityState,
  timestamp = "2026-08-02T12:00:00Z",
): ChatActivity {
  counter += 1;
  const eventId = `event_${String(counter)}`;
  const envelope: EventEnvelope = {
    schema_version: 1,
    event_id: eventId,
    stream_id: "ses_1",
    run_id: "run_1",
    event_type: kind,
    durability: "durable",
    timestamp,
    payload: { kind },
    sequence: counter,
    task_id: null,
    // The point of this whole module: a Chat turn has no graph node.
    graph_node_id: null,
    parent_event_id: null,
  };
  return { key: eventId, eventId, kind, label: kind, state, timestamp, envelope };
}
