import { describe, expect, it } from "vitest";
import type { EventEnvelope, TaskTimelineResponse } from "../../api/types";
import {
  createTimelineState,
  eventTitle,
  findFinalReport,
  findLatestApprovalId,
  collectArtifacts,
  findTaskInputRef,
  mergeTimelineResponse,
  parseTaskInputArtifact,
} from "./workTimeline";

describe("work timeline contract selectors", () => {
  it("merges incremental pages by event_id and keeps delivery order", () => {
    const first = envelope("event_1", "TaskSubmitted", { input_ref: "artifact_input" });
    const second = envelope("event_2", "UnknownFutureEvent");
    const initial = createTimelineState("task_1");

    const afterFirst = mergeTimelineResponse(
      initial,
      timeline("task_1", [first, first], "cursor_1"),
    );
    const afterSecond = mergeTimelineResponse(
      afterFirst,
      timeline("task_1", [first, second], "cursor_2"),
    );

    expect(afterSecond.events.map((event) => event.event_id)).toEqual([
      "event_1",
      "event_2",
    ]);
    expect(afterSecond.cursor).toBe("cursor_2");
    expect(
      mergeTimelineResponse(afterSecond, timeline("another_task", [second], "wrong")),
    ).toBe(afterSecond);
  });

  it("names an event kind it has never heard of instead of hiding it", () => {
    const unknown = envelope("event_unknown", "FutureLedgerFact", {}, "node_research");

    expect(eventTitle(unknown)).toBe("未识别事件：FutureLedgerFact");
  });

  it("collects each artifact once, with the stage that wrote it", () => {
    const artifact = {
      schema_version: 1,
      artifact_id: "art_evidence",
      tenant_id: "tenant_1",
      kind: "evidence_bundle",
      media_type: "application/json",
      size_bytes: 4794,
      sha256: "a".repeat(64),
      filename: "evidence-bundle.json",
    };
    // The same artifact reported twice -- a retried step re-reports what it
    // already wrote, and the rail must not list it twice.
    const events = [
      envelope("event_1", "ToolCompleted", { artifact }, "research_external"),
      envelope("event_2", "ToolCompleted", { artifact }, "research_external"),
      envelope("event_3", "TaskSucceeded", {}),
    ];

    const found = collectArtifacts(events);

    expect(found).toHaveLength(1);
    expect(found[0]?.artifact.artifact_id).toBe("art_evidence");
    expect(found[0]?.graphNodeId).toBe("research_external");
  });

  it("discovers the submitted input and validates its artifact before use", () => {
    const events = [
      envelope("event_1", "TaskSubmitted", { input_ref: "artifact_input" }),
    ];

    expect(findTaskInputRef(events)).toBe("artifact_input");
    expect(
      parseTaskInputArtifact({
        schema_version: 1,
        objective: "Prepare a sourced report",
        max_revisions: 2,
        knowledge_base_id: null,
        wants_report: false,
      }),
    ).toEqual({
      schema_version: 1,
      objective: "Prepare a sourced report",
      max_revisions: 2,
      knowledge_base_id: null,
      wants_report: false,
    });
    // Tasks submitted before the field existed ran under a graph that always
    // exported, so re-submitting one has to keep asking for the report.
    expect(
      parseTaskInputArtifact({
        schema_version: 1,
        objective: "Prepare a sourced report",
        max_revisions: 2,
        knowledge_base_id: null,
      })?.wants_report,
    ).toBe(true);
    expect(
      parseTaskInputArtifact({
        schema_version: 1,
        objective: "",
        max_revisions: 2,
        knowledge_base_id: null,
      }),
    ).toBeNull();
  });

  it("uses the latest approval request only to discover the authoritative record", () => {
    const events = [
      envelope("event_1", "TaskApprovalRequested", { approval_id: "approval_1" }),
      envelope("event_2", "TaskApprovalDecided", {
        approval_id: "approval_1",
        decision: "approved",
        decision_version: 1,
      }),
      envelope("event_3", "TaskApprovalRequested", { approval_id: "approval_2" }),
    ];

    expect(findLatestApprovalId(events)).toBe("approval_2");
  });

  it("accepts only a strictly correlated export followed by TaskSucceeded", () => {
    const artifact = {
      artifact_id: "artifact_report",
      kind: "report",
      media_type: "text/markdown",
      size_bytes: 128,
      sha256: "a".repeat(64),
      filename: "report.md",
    };
    const modelArtifact = envelope("event_model", "ModelCompleted", { artifact });
    const unrelatedCompletion = envelope("event_unrelated", "ToolCompleted", {
      tool_call_id: "tool_unrelated",
      artifact,
    });
    const success = envelope("event_success", "TaskSucceeded");

    expect(findFinalReport([modelArtifact, unrelatedCompletion, success])).toBeNull();

    const proposed = envelope("event_proposed", "ToolProposed", {
      tool_call_id: "tool_export",
      tool_name: "export_artifact",
    });
    const completed = envelope("event_completed", "ToolCompleted", {
      tool_call_id: "tool_export",
      artifact,
    });
    expect(
      findFinalReport([
        proposed,
        { ...completed, run_id: "another_run" },
        success,
      ]),
    ).toBeNull();
    expect(findFinalReport([proposed, completed])).toBeNull();

    expect(findFinalReport([proposed, completed, success])).toEqual({
      artifact,
      toolCallId: "tool_export",
      completedEventId: "event_completed",
      succeededEventId: "event_success",
    });
  });
});

function timeline(
  taskId: string,
  events: EventEnvelope[],
  cursor: string | null,
): TaskTimelineResponse {
  return { task_id: taskId, events, cursor };
}

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
