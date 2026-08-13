import type { EventEnvelope, LocalChatSession } from "../../api/types";
import type { SseFrame, SseQuarantineFrame } from "../../api/sse";
import { describe, expect, it } from "vitest";
import {
  calledToolNames,
  chatReducer,
  initialChatState,
  reduceChatFrame,
  turnToolNames,
  type ChatState,
} from "./model";

const SESSION: LocalChatSession = {
  sessionId: "ses_1",
  title: "Local chat",
  answerMode: "rag",
  knowledgeBaseId: "kb_main",
  createdAt: "2026-08-02T12:00:00Z",
  updatedAt: "2026-08-02T12:00:00Z",
};

function submitted(): ChatState {
  return chatReducer(initialChatState([SESSION]), {
    type: "turnSubmitted",
    input: {
      localId: "local_1",
      sessionId: SESSION.sessionId,
      question: "What changed?",
      answerMode: "rag",
      knowledgeBaseId: SESSION.knowledgeBaseId,
      topK: 8,
      idempotencyKey: "chat:stable-key",
      submittedAt: "2026-08-02T12:00:01Z",
    },
  });
}

function frame(
  kind: string,
  sequence: number,
  payload: Record<string, unknown> = {},
  runId = "run_1",
): SseFrame {
  const envelope: EventEnvelope = {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    stream_id: SESSION.sessionId,
    run_id: runId,
    event_type: kind,
    durability: "durable",
    timestamp: `2026-08-02T12:00:${String(sequence).padStart(2, "0")}Z`,
    payload: { kind, ...payload },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return { id: `cursor_${sequence}`, event: kind, envelope };
}

function quarantine(
  sequence: number,
  overrides: Partial<SseQuarantineFrame["quarantined"]> = {},
): SseQuarantineFrame {
  return {
    id: `cursor_${sequence}`,
    event: "stream.quarantined",
    quarantined: {
      event_id: `evt_${sequence}`,
      event_type: "ModelCompleted",
      schema_version: 1,
      sequence,
      stream_id: SESSION.sessionId,
      ...overrides,
    },
  };
}

describe("chat state machine", () => {
  it("holds an early RunStarted until the HTTP response authoritatively binds its run", () => {
    const result = reduceChatFrame(submitted(), SESSION.sessionId, frame("RunStarted", 1));

    expect(result.accepted).toBe(true);
    expect(result.state.runToTurn.run_1).toBeUndefined();
    expect(result.state.turns.local_1?.runId).toBeUndefined();
    expect(result.state.turns.local_1?.phase).toBe("submitting");
    expect(result.state.orphanEvents.run_1).toHaveLength(1);
  });

  it("does not let an unrelated run overwrite the turn bound by its Ask response", () => {
    let state = reduceChatFrame(
      submitted(),
      SESSION.sessionId,
      frame("RunStarted", 1, {}, "run_old"),
    ).state;
    state = chatReducer(state, {
      type: "askResolved",
      localId: "local_1",
      response: {
        answer: "Answer for the current question",
        citations: [],
        withheld: false,
        grounded: true,
        run_id: "run_current",
        turn_id: "turn_current",
      },
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame(
        "AnswerCommitted",
        2,
        { text: "Answer from another tab", citations: [] },
        "run_old",
      ),
    ).state;

    expect(state.turns.local_1?.runId).toBe("run_current");
    expect(state.turns.local_1?.answer).toBe("Answer for the current question");
    expect(state.runToTurn.run_old).toBeUndefined();
    expect(state.orphanEvents.run_old).toHaveLength(2);
  });

  it("quarantines an existing mapping when an authoritative run conflicts", () => {
    let state = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_old",
    });
    state = chatReducer(state, {
      type: "askResolved",
      localId: "local_1",
      response: {
        answer: "Current response",
        citations: [],
        withheld: false,
        grounded: true,
        run_id: "run_current",
        turn_id: "turn_current",
      },
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame(
        "AnswerCommitted",
        1,
        { text: "Old response", citations: [] },
        "run_old",
      ),
    ).state;

    expect(state.turns.local_1?.phase).toBe("failed");
    expect(state.turns.local_1?.answer).toBeUndefined();
    expect(state.turns.local_1?.runId).toBeUndefined();
    expect(state.runToTurn.run_old).toBeUndefined();
    expect(state.orphanEvents.run_old).toHaveLength(1);
  });

  it("never publishes or retains ModelCompleted candidate text", () => {
    let state = reduceChatFrame(
      submitted(),
      SESSION.sessionId,
      frame("RunStarted", 1),
    ).state;
    state = chatReducer(state, {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame("ModelCompleted", 2, {
        model_call_id: "model_1",
        finish_reason: "stop",
        usage: { input_tokens: 3, output_tokens: 4 },
        text: "PRIVATE CANDIDATE THAT FAILED THE RELEASE FENCE",
      }),
    ).state;

    expect(state.turns.local_1?.phase).toBe("running");
    expect(state.turns.local_1?.answer).toBeUndefined();
    expect(JSON.stringify(state)).not.toContain("PRIVATE CANDIDATE");
    expect(state.turns.local_1?.activities.at(-1)?.detail).toBe("7 tokens · stop");
  });

  it("keeps the prompt on an openable step while still dropping the candidate", () => {
    let state = reduceChatFrame(
      submitted(),
      SESSION.sessionId,
      frame("RunStarted", 1),
    ).state;
    state = chatReducer(state, {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame("ModelStarted", 2, {
        model_call_id: "model_1",
        model_profile: "main",
        model_id: "deepseek-chat",
        prompt_preview: "[system]\nAnswer from the evidence.\n\n[user]\nWhat changed?",
      }),
    ).state;
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame("ModelCompleted", 3, {
        model_call_id: "model_1",
        finish_reason: "stop",
        usage: { input_tokens: 3, output_tokens: 4 },
        text: "PRIVATE CANDIDATE THAT FAILED THE RELEASE FENCE",
      }),
    ).state;

    const serialized = JSON.stringify(state);
    // What the model was given survives, so a step can be opened and read.
    expect(serialized).toContain("Answer from the evidence.");
    // What the model produced does not, until the fence publishes it.
    expect(serialized).not.toContain("PRIVATE CANDIDATE");
  });

  it("holds an orphan terminal event and replays it after the HTTP run binding", () => {
    const terminal = frame("AnswerCommitted", 1, {
      text: "Checked answer",
      citations: [
        {
          chunk_id: "chunk_1",
          document_id: "doc_1",
          document_version: "rev_1",
          locator: {},
        },
      ],
    });
    const held = reduceChatFrame(submitted(), SESSION.sessionId, terminal);
    expect(held.state.turns.local_1?.answer).toBeUndefined();
    expect(held.state.orphanEvents.run_1).toHaveLength(1);

    const bound = chatReducer(held.state, {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
      turnId: "turn_1",
    });
    expect(bound.turns.local_1?.phase).toBe("committed");
    expect(bound.turns.local_1?.answer).toBe("Checked answer");
    expect(bound.turns.local_1?.citations).toHaveLength(1);
    expect(bound.orphanEvents.run_1).toBeUndefined();

    const replay = reduceChatFrame(bound, SESSION.sessionId, terminal);
    expect(replay.duplicate).toBe(true);
    expect(replay.state).toBe(bound);
    expect(bound.turns.local_1?.activities.filter((item) => item.key === "answer")).toHaveLength(1);
  });

  it("allows only the safe AskResponse to terminate without an SSE terminal", () => {
    const state = chatReducer(submitted(), {
      type: "askResolved",
      localId: "local_1",
      response: {
        answer: "Released by the HTTP publication path",
        citations: [],
        withheld: false,
        grounded: true,
        run_id: "run_1",
        turn_id: "turn_1",
      },
    });

    expect(state.turns.local_1?.phase).toBe("committed");
    expect(state.turns.local_1?.answer).toBe("Released by the HTTP publication path");
  });

  it("fails closed when AnswerWithheld races a committed HTTP response", () => {
    let state = chatReducer(submitted(), {
      type: "askResolved",
      localId: "local_1",
      response: {
        answer: "Committed response",
        citations: [
          {
            chunk_id: "chunk_1",
            document_id: "doc_1",
            document_version: "rev_1",
            locator: {},
          },
        ],
        withheld: false,
        grounded: true,
        run_id: "run_1",
        turn_id: "turn_1",
      },
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame("AnswerWithheld", 1, {
        text: "Safe replacement",
        reason_code: "sources_changed",
      }),
    ).state;

    expect(state.turns.local_1?.phase).toBe("withheld");
    expect(state.turns.local_1?.answer).toBe("Safe replacement");
    expect(state.turns.local_1?.citations).toEqual([]);
    expect(state.turns.local_1?.activities.filter((item) => item.key === "answer")).toHaveLength(1);
  });

  it("keeps the original idempotency key when a failed turn retries", () => {
    let state = chatReducer(submitted(), {
      type: "askRejected",
      localId: "local_1",
      error: "connection reset",
    });
    state = chatReducer(state, { type: "turnRetrying", localId: "local_1" });

    expect(state.turns.local_1?.phase).toBe("submitting");
    expect(state.turns.local_1?.idempotencyKey).toBe("chat:stable-key");
  });

  it("rejects a frame whose SSE event name disagrees with the envelope", () => {
    const mismatched = frame("AnswerCommitted", 1, { text: "no", citations: [] });
    mismatched.event = "ModelCompleted";
    const result = reduceChatFrame(submitted(), SESSION.sessionId, mismatched);

    expect(result.accepted).toBe(false);
    expect(result.state).toEqual(submitted());
  });
});

/**
 * A tool row has to say which tool, for what, and how it went -- in one line.
 *
 * Measured on a real turn before this: three rows reading 工具调用 / 工具执行完成
 * with "512 B 参数" and "1841 ms" beside them. Every fact true, and the reader
 * still could not tell a corpus search from a web search, nor what was searched.
 */
describe("a tool call as one readable line", () => {
  function bound(...frames: SseFrame[]): ChatState {
    let state = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    for (const item of frames) {
      state = reduceChatFrame(state, SESSION.sessionId, item).state;
    }
    return state;
  }

  function activityFor(state: ChatState, key: string) {
    return state.turns.local_1?.activities.find((item) => item.key === key);
  }

  it("shows what the call was for, not how many bytes the arguments were", () => {
    const state = bound(
      frame("ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        argument_bytes: 512,
        argument_preview: JSON.stringify({ query: "美元兑人民币汇率 今日" }),
      }),
    );

    const activity = activityFor(state, "tool:call_1");
    expect(activity?.label).toBe("web_search");
    expect(activity?.detail).toBe("美元兑人民币汇率 今日");
  });

  it("falls back to the byte count when the deployment records no arguments", () => {
    // The control. `record_step_inputs` is off by default, and a summary that
    // silently went blank there would trade one uninformative line for none.
    const state = bound(
      frame("ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        argument_bytes: 512,
      }),
    );

    expect(activityFor(state, "tool:call_1")?.detail).toBe("512 B 参数");
  });

  it("keeps one call on one row from proposal through completion", () => {
    // `ToolStarted` used to fall through to the generic tail, key itself on
    // `event_id` and render a second line reading the raw event name beside a
    // row that already said the same thing.
    const state = bound(
      frame("ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        argument_preview: JSON.stringify({ query: "北京今天天气" }),
      }),
      frame("ToolStarted", 2, { tool_call_id: "call_1", tool_name: "web_search" }),
    );
    const toolRows = (state.turns.local_1?.activities ?? []).filter((item) =>
      item.key.startsWith("tool:"),
    );

    expect(toolRows).toHaveLength(1);
    // And the query survives the overwrite, since only the proposal carried it.
    expect(toolRows[0]?.envelope.payload.argument_preview).toContain("北京今天天气");
  });

  it("keeps the tool's name on the row after it finishes", () => {
    const state = bound(
      frame("ToolProposed", 1, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        argument_preview: JSON.stringify({ query: "汇率" }),
      }),
      frame("ToolCompleted", 2, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        duration_ms: 1841,
      }),
    );

    const activity = activityFor(state, "tool:call_1");
    expect(activity?.label).toBe("web_search");
    expect(activity?.detail).toBe("1841 ms");
  });

  it("keeps the tool's name on a failure that does not carry one", () => {
    // Measured against the real server: `ToolFailed` carries `tool_call_id`
    // and `error`, and no `tool_name`. It shares a key with the proposal, so
    // without carry-forward a failed call rendered as a nameless
    // "工具执行失败" and the capability row above it called `web_search`
    // never-used -- on the very turn it had just failed in.
    const state = bound(
      frame("ToolProposed", 1, { tool_call_id: "call_1", tool_name: "web_search" }),
      frame("ToolFailed", 2, {
        tool_call_id: "call_1",
        error: { code: "provider_unavailable", message: "refused=5" },
      }),
    );
    const activities = state.turns.local_1?.activities ?? [];

    expect(activityFor(state, "tool:call_1")?.label).toBe("web_search");
    expect(calledToolNames(activities)).toEqual(["web_search"]);
  });

  it("puts a failure's message on the row, because the code is ambiguous", () => {
    // `provider_unavailable` is the same code for "no provider configured" and
    // "found 19 pages and could read none of them". Showing the code alone is
    // what made a proxy misconfiguration read as a missing feature.
    const state = bound(
      frame("ToolFailed", 1, {
        tool_call_id: "call_1",
        tool_name: "web_search",
        error: {
          code: "provider_unavailable",
          message:
            "web search found 19 page(s) and none could be read from this deployment (refused=19)",
        },
      }),
    );

    const activity = activityFor(state, "tool:call_1");
    expect(activity?.state).toBe("failed");
    expect(activity?.detail).toContain("19 page(s)");
    expect(activity?.detail).toContain("refused=19");
  });
});

describe("the tools a turn could reach", () => {
  it("reads the available names off RunStarted and marks the ones called", () => {
    let state = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    for (const item of [
      frame("RunStarted", 1, { tool_names: ["knowledge_search", "web_search"] }),
      frame("ToolProposed", 2, { tool_call_id: "c1", tool_name: "web_search" }),
    ]) {
      state = reduceChatFrame(state, SESSION.sessionId, item).state;
    }
    const activities = state.turns.local_1?.activities ?? [];

    expect(turnToolNames(activities)).toEqual(["knowledge_search", "web_search"]);
    // Only what ran. A capability list that marked everything as used would
    // say nothing, and one that marked nothing would hide the turn's work.
    expect(calledToolNames(activities)).toEqual(["web_search"]);
  });

  it("still knows the tools after RunCompleted overwrites RunStarted", () => {
    // The two share a `run:` key so one run is one row, and the completion
    // replaces the start -- taking `tool_names` with it. Measured in the
    // browser before `carryForward` covered this field: the capability row
    // rendered on a running turn and vanished the moment it finished, which is
    // precisely when a reader goes looking for it.
    let state = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    for (const item of [
      frame("RunStarted", 1, { tool_names: ["web_search"] }),
      frame("RunCompleted", 2, { stop_reason: "completed" }),
    ]) {
      state = reduceChatFrame(state, SESSION.sessionId, item).state;
    }
    const activities = state.turns.local_1?.activities ?? [];

    expect(activities.filter((item) => item.key === "run:run_1")).toHaveLength(1);
    expect(turnToolNames(activities)).toEqual(["web_search"]);
  });

  it("reports no tools for a turn that was granted none", () => {
    // The control: the direct and fixed shapes are toolless by construction,
    // and the row must stay absent rather than render an empty capability list.
    let state = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    state = reduceChatFrame(
      state,
      SESSION.sessionId,
      frame("RunStarted", 1, { tool_names: [] }),
    ).state;

    expect(turnToolNames(state.turns.local_1?.activities ?? [])).toEqual([]);
  });
});

describe("where a citation says it came from", () => {
  function citationsWith(locator: unknown) {
    const bound = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    const state = reduceChatFrame(
      bound,
      SESSION.sessionId,
      frame("AnswerCommitted", 1, {
        text: "答案正文",
        citations: [
          {
            chunk_id: "chunk_1",
            document_id: "doc_1",
            document_version: "rev_1",
            locator,
          },
        ],
      }),
    ).state;
    return state.turns.local_1?.citations ?? [];
  }

  it("keeps the page and the chunk ordinal instead of an opaque blob", () => {
    // Both were already on the wire and both stopped at the reducer, which
    // stored the locator as an unread object. The chip could only ever show the
    // chunk id, which locates a citation in the index and nowhere a reader can
    // go.
    const [citation] = citationsWith({
      page: 3,
      paragraph: 12,
      char_start: 40,
      char_end: 900,
    });

    expect(citation?.locator.page).toBe(3);
    expect(citation?.locator.paragraph).toBe(12);
    // Deliberately unread. They are computed at ingestion and the index stores
    // only `ordinal` and `page` (`ports/vector_index.py`), so on a real
    // citation these are never present -- a parsed field that can only render
    // empty is a promise the data cannot keep.
    expect(citation?.locator.char_start).toBeUndefined();
    expect(citation?.locator.char_end).toBeUndefined();
  });

  it("leaves a pageless source pageless", () => {
    // Markdown and txt have no pages, and the server sends null rather than 1
    // for exactly that reason. Filling it in here would claim a location
    // nothing established.
    const [citation] = citationsWith({ page: null, paragraph: 0 });

    expect(citation?.locator.page).toBeUndefined();
    expect(citation?.locator.paragraph).toBe(0);
  });

  it("drops a position outside the range the server itself enforces", () => {
    // `page >= 1` and `paragraph >= 0` are validated in `domain/context.py`, so
    // 第 0 页 can only come from something that is not this server -- and it is
    // not a page anyone can turn to.
    const [citation] = citationsWith({ page: 0, paragraph: -1 });

    expect(citation?.locator).toEqual({});
  });
});

describe("how a terminal run event explains itself", () => {
  function terminal(kind: string, payload: Record<string, unknown>) {
    const bound = chatReducer(submitted(), {
      type: "runBound",
      localId: "local_1",
      runId: "run_1",
    });
    return reduceChatFrame(bound, SESSION.sessionId, frame(kind, 1, payload)).state.turns
      .local_1;
  }

  it("says why a run failed, from the field RunFailed actually carries", () => {
    // `RunFailed` carries `error`; `reason_code` belongs to its neighbours
    // (`domain/events.py`). Sharing their branch meant reading a field this
    // event does not have, so the label fell through to the event type: the
    // screen said "RunFailed" and gave no cause at all.
    const turn = terminal("RunFailed", {
      error: { code: "provider_unavailable", message: "模型连续三次超时，未能完成本轮" },
      stop_reason: "error",
    });

    expect(turn?.phase).toBe("failed");
    expect(turn?.activities.at(-1)?.label).toBe("运行失败");
    expect(turn?.error).toBe("模型连续三次超时，未能完成本轮");
    expect(turn?.error).not.toContain("RunFailed");
  });

  it("falls back to the code when the failure carries no message", () => {
    const turn = terminal("RunFailed", { error: { code: "budget_exhausted", message: "" } });

    expect(turn?.error).toBe("budget_exhausted");
  });

  it("falls back to the label when the failure says nothing at all", () => {
    // The floor. Whatever the server omits, the line a user reads is a
    // sentence -- never an empty notice and never "undefined".
    const turn = terminal("RunFailed", { error: {} });

    expect(turn?.activities.at(-1)?.detail).toBeUndefined();
    expect(turn?.error).toBe("运行失败");
  });

  it("control: cancellation still reads its own reason_code", () => {
    // `RunCancelled` was never broken -- it has the field the old shared branch
    // read. This pins that the split did not take its reason away with it.
    const turn = terminal("RunCancelled", { reason_code: "cancel_requested" });

    expect(turn?.phase).toBe("failed");
    expect(turn?.activities.at(-1)?.label).toBe("运行已取消");
    expect(turn?.error).toBe("cancel_requested");
  });

  it("says why a turn expired, from the code ChatTurnExpired actually carries", () => {
    // The same defect as `RunFailed`, one event over: `ChatTurnExpired` has
    // `error_code`, not `reason_code` (`domain/events.py`), so while it shared
    // the cancellation branch its cause was read from a field it never has and
    // the line fell through to the bare event name.
    const turn = terminal("ChatTurnExpired", {
      stop_reason: "deadline",
      error_code: "stale_execution",
      retryable: false,
    });

    expect(turn?.phase).toBe("failed");
    expect(turn?.activities.at(-1)?.label).toBe("本轮已过期");
    expect(turn?.error).toBe("stale_execution");
    expect(turn?.error).not.toContain("ChatTurnExpired");
  });
});

describe("positions the stream could not deliver", () => {
  it("records the position against the session without inventing an event", () => {
    const result = reduceChatFrame(submitted(), SESSION.sessionId, quarantine(2));

    expect(result.accepted).toBe(true);
    expect(result.state.quarantinedSequences[SESSION.sessionId]).toEqual([2]);
    // Nothing was applied, because there is nothing to apply: no step, no
    // orphaned event, and a turn that has not moved.
    expect(result.state.turns.local_1?.activities).toEqual([]);
    expect(result.state.orphanEvents).toEqual({});
    expect(result.state.turns.local_1?.phase).toBe("submitting");
  });

  it("counts a replayed notice once", () => {
    const first = reduceChatFrame(submitted(), SESSION.sessionId, quarantine(2));
    const second = reduceChatFrame(first.state, SESSION.sessionId, quarantine(2));

    expect(second.duplicate).toBe(true);
    expect(second.state).toBe(first.state);
    expect(second.state.quarantinedSequences[SESSION.sessionId]).toEqual([2]);
  });

  it("lists the positions in stream order however they arrive", () => {
    // A reconnect can replay an earlier hole after a later one, and a list a
    // reader compares against the log should read in the log's order.
    let state = reduceChatFrame(submitted(), SESSION.sessionId, quarantine(7)).state;
    state = reduceChatFrame(state, SESSION.sessionId, quarantine(3)).state;

    expect(state.quarantinedSequences[SESSION.sessionId]).toEqual([3, 7]);
  });

  it("refuses a notice announcing another stream's position", () => {
    const before = submitted();
    const result = reduceChatFrame(
      before,
      SESSION.sessionId,
      quarantine(2, { stream_id: "ses_other" }),
    );

    expect(result.accepted).toBe(false);
    expect(result.state).toBe(before);
  });
});
