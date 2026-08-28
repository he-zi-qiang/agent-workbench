import { describe, expect, it } from "vitest";
import type { EventEnvelope, TaskTimelineResponse } from "../../api/types";
import {
  createTimelineState,
  eventTitle,
  findDeliverable,
  findDraftText,
  findFinalReport,
  findGraphChoice,
  findLatestApprovalId,
  collectArtifacts,
  collectWorkspaceWrites,
  findTaskInputRef,
  findTaskIntent,
  locateTimelineGaps,
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

  it("keeps every position the server could not deliver, across pages", () => {
    const first = envelope("event_1", "TaskSubmitted", {}, null, 1);
    const later = envelope("event_3", "RunStarted", {}, null, 3);
    const initial = createTimelineState("task_1");

    const afterFirst = mergeTimelineResponse(
      initial,
      timeline("task_1", [first], "stream_1:2", [2]),
    );
    // The second page re-reports position 2, the way an overlapping re-read
    // does, and names one more. Counted instead of named, this would read as
    // four holes in a history that has two.
    const afterSecond = mergeTimelineResponse(
      afterFirst,
      timeline("task_1", [later], "stream_1:5", [5, 2]),
    );

    expect(afterFirst.skippedSequences).toEqual([2]);
    expect(afterSecond.skippedSequences).toEqual([2, 5]);
    // The cursor still moves past the damage. The server pushes callers past a
    // row it could not decode on purpose -- stalling in front of it would mean
    // nobody ever advances -- so accumulating holes must not fight that.
    expect(afterSecond.cursor).toBe("stream_1:5");
    // Another Task's page cannot pin its damage on this one.
    expect(
      mergeTimelineResponse(afterSecond, timeline("other_task", [], "x", [9])),
    ).toBe(afterSecond);
  });

  it("carries a clean page's claim of completeness rather than inventing damage", () => {
    // The control group. An implementation that always has something to report
    // dies here, and so does one that lets a stray value through: the empty
    // list is a positive claim, and repeating a malformed number back to the
    // reader would be inventing a hole rather than reporting one.
    const initial = createTimelineState("task_1");

    const afterFirst = mergeTimelineResponse(
      initial,
      timeline("task_1", [envelope("event_1", "TaskSubmitted", {}, null, 1)], "s:1", []),
    );
    const afterSecond = mergeTimelineResponse(
      afterFirst,
      timeline("task_1", [envelope("event_2", "RunStarted", {}, null, 2)], "s:2", [
        0,
        -1,
        1.5,
        Number.NaN,
      ]),
    );

    expect(afterSecond.skippedSequences).toEqual([]);
    // And the same array throughout, so the memo the page builds over it is
    // not re-run by every poll of a healthy Task.
    expect(afterSecond.skippedSequences).toBe(initial.skippedSequences);
  });

  it("places each undelivered position between the events that did arrive", () => {
    const submitted = envelope("event_1", "TaskSubmitted", {}, null, 1);
    const started = envelope("event_3", "RunStarted", {}, null, 3);

    expect(locateTimelineGaps([submitted, started], [4, 2])).toEqual([
      { sequence: 2, before: submitted, after: started },
      // Nothing readable came back after 4, so there is no later step to name
      // it against -- and the gap says so instead of guessing one.
      { sequence: 4, before: started, after: null },
    ]);
  });

  it("stops calling a position a hole once a re-read delivered it", () => {
    // A repeated read can decode a row an earlier pass could not. The client is
    // holding that event now, so still calling it missing would be the same
    // lie pointed the other way.
    const submitted = envelope("event_1", "TaskSubmitted", {}, null, 1);
    const recovered = envelope("event_2", "RunStarted", {}, null, 2);

    expect(locateTimelineGaps([submitted, recovered], [2])).toEqual([]);
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

  it("reads the pipeline a retry must ask for from the submission event", () => {
    // The choice is deliberately not in the input artifact -- which pipeline
    // runs a Task is a property of the submission -- so a faithful retry has
    // to read it back from TaskSubmitted's recorded version.
    expect(
      findGraphChoice([
        envelope("event_1", "TaskSubmitted", { graph_version: "v1" }),
      ]),
    ).toBe("research");
    expect(
      findGraphChoice([
        envelope("event_1", "TaskSubmitted", { graph_version: "v2_general" }),
      ]),
    ).toBe("general");
    // A version this client cannot name: the retry omits the field and takes
    // the deployment default rather than guessing a shape.
    expect(
      findGraphChoice([
        envelope("event_1", "TaskSubmitted", { graph_version: "v9_future" }),
      ]),
    ).toBeNull();
    expect(findGraphChoice([envelope("event_1", "TaskClaimed")])).toBeNull();
  });

  it("reads shape provenance from the submission event, and refuses malformed claims", () => {
    expect(
      findTaskIntent([
        envelope("event_1", "TaskSubmitted", {
          graph_version: "v2_general",
          intent: {
            graph_decided_by: "model",
            wants_report_decided_by: "default",
            reason: "要把事做完",
          },
        }),
      ]),
    ).toEqual({
      graph_decided_by: "model",
      wants_report_decided_by: "default",
      reason: "要把事做完",
    });
    // Tasks submitted before the field existed claim nothing.
    expect(
      findTaskIntent([
        envelope("event_1", "TaskSubmitted", { graph_version: "v1" }),
      ]),
    ).toBeNull();
    // A malformed block is provenance this client cannot vouch for: shown as
    // nothing rather than as fact.
    expect(
      findTaskIntent([
        envelope("event_1", "TaskSubmitted", {
          intent: { graph_decided_by: "somebody" },
        }),
      ]),
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

  it("leads with the document a Task rendered, and the report when it did not", () => {
    /**
     * A Word Task produces both files, and the page used to headline the wrong
     * one: `export_artifact` always writes `report.md`, so a run that had
     * rendered a .docx showed a Markdown page called "Task report" while the
     * document sat in the attachment rail behind it.
     *
     * The research half is the control, and it is the one that matters: a Task
     * whose whole product *is* the written report must be left exactly as it
     * was, or this fix trades one wrong headline for another.
     */
    const report = {
      artifact_id: "artifact_report",
      kind: "report",
      media_type: "text/markdown",
      size_bytes: 128,
      sha256: "a".repeat(64),
      filename: "report.md",
    };
    const docx = {
      artifact_id: "artifact_docx",
      kind: "document",
      media_type:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      size_bytes: 37_000,
      sha256: "b".repeat(64),
      filename: "mcp-result.docx",
    };
    const exportProposed = envelope("event_proposed", "ToolProposed", {
      tool_call_id: "tool_export",
      tool_name: "export_artifact",
    });
    const exportCompleted = envelope("event_completed", "ToolCompleted", {
      tool_call_id: "tool_export",
      artifact: report,
    });
    const success = envelope("event_success", "TaskSucceeded");

    // Control: no document rendered, so the export is still what leads.
    expect(findDeliverable([exportProposed, exportCompleted, success])).toEqual(
      report,
    );

    const rendered = envelope("event_docx", "ToolCompleted", {
      tool_call_id: "tool_render",
      artifact: docx,
    });
    expect(
      findDeliverable([rendered, exportProposed, exportCompleted, success]),
    ).toEqual(docx);

    // Evidence is not a deliverable: a research Task collects it every run, and
    // preferring it would break the control above for every Task on the graph.
    const evidence = envelope("event_evidence", "ToolCompleted", {
      tool_call_id: "tool_search",
      artifact: {
        artifact_id: "artifact_evidence",
        kind: "evidence_bundle",
        media_type: "application/json",
        size_bytes: 900,
        sha256: "c".repeat(64),
        filename: "evidence.json",
      },
    });
    expect(
      findDeliverable([evidence, exportProposed, exportCompleted, success]),
    ).toEqual(report);

    // A re-render after a reviewer's note supersedes the draft that prompted
    // it; the superseded file is still in the rail.
    const reRendered = envelope("event_docx_2", "ToolCompleted", {
      tool_call_id: "tool_render_2",
      artifact: { ...docx, artifact_id: "artifact_docx_2" },
    });
    expect(findDeliverable([rendered, reRendered, success])?.artifact_id).toBe(
      "artifact_docx_2",
    );

    // Nothing produced at all stays null rather than inventing a headline.
    expect(findDeliverable([success])).toBeNull();
  });

  it("does not headline a document the Task merely fetched", () => {
    // A research Task pulls a PDF off the web through the web MCP server. It is
    // a real artifact and a real DOCUMENT_MEDIA_TYPE, and it arrives late -- so
    // under "last document wins" it took the headline, under the name the MCP
    // result mapping gives an unnamed payload. It is somebody else's document.
    const report = {
      artifact_id: "artifact_report",
      kind: "report",
      media_type: "text/markdown",
      size_bytes: 128,
      sha256: "a".repeat(64),
      filename: "report.md",
    };
    const exportProposed = envelope("event_proposed", "ToolProposed", {
      tool_call_id: "tool_export",
      tool_name: "export_artifact",
    });
    const exportCompleted = envelope(
      "event_export",
      "ToolCompleted",
      { tool_call_id: "tool_export", artifact: report },
      "export",
    );
    const success = envelope("event_success", "TaskSucceeded");
    const fetched = envelope(
      "event_fetch",
      "ToolCompleted",
      {
        tool_call_id: "tool_web",
        artifact: {
          artifact_id: "artifact_fetched",
          kind: "tool_result",
          media_type: "application/pdf",
          size_bytes: 900_000,
          sha256: "d".repeat(64),
          filename: "mcp-result.bin",
        },
      },
      "research_external",
    );

    expect(
      findDeliverable([exportProposed, exportCompleted, success, fetched]),
    ).toEqual(report);

    // The same bytes produced by a stage that *makes* things still headline.
    // The rule is about provenance, never about the media type.
    const rendered = { ...fetched, graph_node_id: "work", event_id: "event_work" };
    expect(
      findDeliverable([exportProposed, exportCompleted, success, rendered])
        ?.artifact_id,
    ).toBe("artifact_fetched");
  });

  it("does not headline a document nothing on this page can show", () => {
    // .xlsx is in DOCUMENT_MEDIA_TYPES because a deployment may one day render
    // it. None does today, so promoting it spent the most prominent place on
    // the page to say 这个类型只能下载查看 -- while the report that *can* be
    // read sat behind it.
    const report = {
      artifact_id: "artifact_report",
      kind: "report",
      media_type: "text/markdown",
      size_bytes: 128,
      sha256: "a".repeat(64),
      filename: "report.md",
    };
    const sheet = envelope(
      "event_sheet",
      "ToolCompleted",
      {
        tool_call_id: "tool_sheet",
        artifact: {
          artifact_id: "artifact_sheet",
          kind: "document",
          media_type:
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          size_bytes: 4096,
          sha256: "e".repeat(64),
          filename: "table.xlsx",
        },
      },
      "work",
    );
    const exportProposed = envelope("event_proposed", "ToolProposed", {
      tool_call_id: "tool_export",
      tool_name: "export_artifact",
    });
    const exportCompleted = envelope(
      "event_export",
      "ToolCompleted",
      { tool_call_id: "tool_export", artifact: report },
      "export",
    );
    const success = envelope("event_success", "TaskSucceeded");

    expect(
      findDeliverable([exportProposed, exportCompleted, success, sheet]),
    ).toEqual(report);
  });

  it("lets a produced page headline, because running it is the only way to accept it", () => {
    const page = envelope(
      "event_page",
      "ToolCompleted",
      {
        tool_call_id: "tool_page",
        artifact: {
          artifact_id: "artifact_page",
          kind: "document",
          media_type: "text/html",
          size_bytes: 9000,
          sha256: "f".repeat(64),
          filename: "dashboard.html",
        },
      },
      "work",
    );

    expect(findDeliverable([page])?.artifact_id).toBe("artifact_page");
  });

  it("counts a model call's own output as something the Task produced", () => {
    // `payload.output_ref` is the other door into the artifact store, and the
    // rail only ever watched `payload.artifact` -- so a draft a synthesize or
    // work node wrote was reachable only three disclosures deep in the step
    // detail, while the rail claimed to list what the Task produced.
    const draft = envelope(
      "event_model",
      "ModelCompleted",
      {
        output_ref: {
          artifact_id: "artifact_draft",
          kind: "draft",
          media_type: "text/markdown",
          size_bytes: 2048,
          sha256: "9".repeat(64),
          filename: "draft.md",
        },
      },
      "synthesize",
    );

    expect(collectArtifacts([draft]).map((one) => one.artifact.artifact_id)).toEqual([
      "artifact_draft",
    ]);
  });

  it("names the working-set files a Task wrote, grouped by the stage", () => {
    // ADR-063 has published these unconditionally since it landed -- outside
    // the `record_step_inputs` gate, so a default deployment carries them --
    // and this page read none of it. A Task that rendered three files into its
    // working set showed the reader nothing: no names, no count, no sentence.
    const wrote = (id: string, node: string, names: string[]) =>
      envelope(id, "ToolCompleted", { workspace_writes: names }, node);

    const groups = collectWorkspaceWrites([
      wrote("e1", "work", ["draft.md", "helper.py"]),
      // Same stage running twice keeps one group, and a name it wrote twice is
      // one fact reported once.
      wrote("e2", "work", ["draft.md", "chart.png"]),
      wrote("e3", "export", ["report.md"]),
    ]);

    expect(groups).toEqual([
      {
        graphNodeId: "work",
        names: ["draft.md", "helper.py", "chart.png"],
        refs: new Map(),
      },
      { graphNodeId: "export", names: ["report.md"], refs: new Map() },
    ]);
  });

  it("把名字配到它当时绑的那件 artifact 上（ADR-088）", () => {
    // 按 filename 配，不按下标——ADR-088 §4。少发一个引用只该让那一行打不开，
    // 而不是让它后面每一行都指向错的文件。
    const ref = (id: string, filename: string) => ({
      schema_version: 1,
      artifact_id: id,
      tenant_id: "tenant_local",
      kind: "workspace_entry",
      media_type: "text/markdown",
      size_bytes: 12,
      sha256: "a".repeat(64),
      filename,
    });

    const groups = collectWorkspaceWrites([
      envelope(
        "e1",
        "ToolCompleted",
        {
          // 顺序与 refs 故意不同。
          workspace_writes: ["a.md", "b.md"],
          workspace_write_refs: [ref("art_b", "b.md"), ref("art_a", "a.md")],
        },
        "work",
      ),
    ]);

    const only = groups[0];
    if (only === undefined) throw new Error("expected one group");
    expect(only.names).toEqual(["a.md", "b.md"]);
    expect(only.refs.get("a.md")?.artifact_id).toBe("art_a");
    expect(only.refs.get("b.md")?.artifact_id).toBe("art_b");
  });

  it("没有引用的名字照旧只是名字，不会变成一颗打不开的按钮", () => {
    // ADR-088 之前写下的事件不带引用，那些行必须原样保留——列出来、不可点。
    const groups = collectWorkspaceWrites([
      envelope("e1", "ToolCompleted", { workspace_writes: ["old.md"] }, "work"),
    ]);

    const only = groups[0];
    if (only === undefined) throw new Error("expected one group");
    expect(only.names).toEqual(["old.md"]);
    expect(only.refs.size).toBe(0);
  });

  it("同一个名字被写第二次时，绑的是较新的那件", () => {
    // manifest 一个名字只绑一件 artifact，最新的那次写入就是这个名字的含义。
    const ref = (id: string) => ({
      schema_version: 1,
      artifact_id: id,
      tenant_id: "tenant_local",
      kind: "workspace_entry",
      media_type: "text/markdown",
      size_bytes: 12,
      sha256: "b".repeat(64),
      filename: "draft.md",
    });

    const groups = collectWorkspaceWrites([
      envelope(
        "e1",
        "ToolCompleted",
        { workspace_writes: ["draft.md"], workspace_write_refs: [ref("art_v1")] },
        "work",
      ),
      envelope(
        "e2",
        "ToolCompleted",
        { workspace_writes: ["draft.md"], workspace_write_refs: [ref("art_v2")] },
        "work",
      ),
    ]);

    const only = groups[0];
    if (only === undefined) throw new Error("expected one group");
    expect(only.refs.get("draft.md")?.artifact_id).toBe("art_v2");
  });

  it("坏掉的引用被丢掉，而不是被送到一个下载地址上", () => {
    const groups = collectWorkspaceWrites([
      envelope(
        "e1",
        "ToolCompleted",
        {
          workspace_writes: ["x.md"],
          // sha256 不合法：走的是这个页面上每个引用都要过的同一个校验器。
          workspace_write_refs: [
            {
              artifact_id: "art_x",
              kind: "workspace_entry",
              media_type: "text/markdown",
              size_bytes: 1,
              sha256: "not-a-digest",
              filename: "x.md",
            },
          ],
        },
        "work",
      ),
    ]);

    const only = groups[0];
    if (only === undefined) throw new Error("expected one group");
    expect(only.names).toEqual(["x.md"]);
    expect(only.refs.size).toBe(0);
  });

  it("keeps one name in two stages as two facts", () => {
    // Not deduplicated across groups: two stages writing the same name means
    // the second overwrote the first, and collapsing them hides that.
    const groups = collectWorkspaceWrites([
      envelope("e1", "ToolCompleted", { workspace_writes: ["report.md"] }, "work"),
      envelope("e2", "ToolCompleted", { workspace_writes: ["report.md"] }, "export"),
    ]);

    expect(groups.map((group) => group.graphNodeId)).toEqual(["work", "export"]);
    expect(groups.every((group) => group.names.includes("report.md"))).toBe(true);
  });

  it("ignores a payload that is not a list of names", () => {
    // Off-the-wire JSON. A non-string here would render as `[object Object]`
    // in a file list, and an absent field is the ordinary case for every event
    // that is not a tool completion.
    expect(
      collectWorkspaceWrites([
        envelope("e1", "ToolCompleted", {}, "work"),
        envelope("e2", "ToolCompleted", { workspace_writes: "report.md" }, "work"),
        envelope("e3", "ToolCompleted", { workspace_writes: [1, "", null] }, "work"),
        // A failed call carries no such field at all (ADR-063 §4).
        envelope("e4", "ToolFailed", { workspace_writes: ["ghost.md"] }, "work"),
      ]),
    ).toEqual([]);
  });

  it("reads the draft from whichever graph's drafting node wrote it", () => {
    const v1 = envelope(
      "event_v1",
      "ModelCompleted",
      { text: "v1 的草稿" },
      "synthesize",
    );
    // The v2 graph drafts in `work`, and reading only `synthesize` left a v2
    // Task that produced no file showing "没有产出内容" while its answer sat
    // in the timeline.
    const v2 = envelope("event_v2", "ModelCompleted", { text: "v2 的草稿" }, "work");

    expect(findDraftText([v1])).toBe("v1 的草稿");
    expect(findDraftText([v2])).toBe("v2 的草稿");
  });

  it("skips the tool-calling turns of a drafting loop and the reviewer", () => {
    // A timeline still arriving: the drafting loop's latest turn spent itself
    // on a tool call and so carries no text, while the answering turn has not
    // been polled yet. The draft the page already has is what it should show.
    const midStream = [
      envelope("event_answer", "ModelCompleted", { text: "最终答复" }, "work"),
      envelope("event_tool_turn", "ModelCompleted", { text: "" }, "work"),
      envelope("event_blank_turn", "ModelCompleted", { text: "  \n " }, "work"),
    ];

    expect(findDraftText(midStream)).toBe("最终答复");

    // The reviewer's structured verdict is not the draft, and is the only
    // `ModelCompleted` some Tasks have after the gate opens.
    const review = envelope(
      "event_review",
      "ModelCompleted",
      { text: '{"ok":true}' },
      "review",
    );

    expect(findDraftText([...midStream, review])).toBe("最终答复");
    expect(findDraftText([review])).toBeNull();
  });
});

function timeline(
  taskId: string,
  events: EventEnvelope[],
  cursor: string | null,
  // Defaulted to the empty list rather than omitted, because that is what the
  // server sends for a complete page -- a fixture without it would be a server
  // this client will never meet.
  skipped: number[] = [],
): TaskTimelineResponse {
  return { task_id: taskId, events, cursor, skipped_sequences: skipped };
}

function envelope(
  eventId: string,
  kind: string,
  payload: Record<string, unknown> = {},
  graphNodeId: string | null = null,
  sequence: number | null = null,
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
    sequence,
    task_id: "task_1",
    graph_node_id: graphNodeId,
    parent_event_id: null,
  };
}
