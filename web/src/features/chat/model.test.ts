import type { EventEnvelope, LocalChatSession } from "../../api/types";
import type { SseFrame } from "../../api/sse";
import { describe, expect, it } from "vitest";
import {
  chatReducer,
  initialChatState,
  reduceChatFrame,
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
