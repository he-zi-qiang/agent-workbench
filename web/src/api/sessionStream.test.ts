import {
  isDegradedFrame,
  isQuarantineFrame,
  type SseChunkFrame,
} from "./sse";
import type { PrincipalIdentity } from "./types";
import { afterEach, describe, expect, it, vi } from "vitest";
import { streamSession, type FrameAcceptance } from "./sessionStream";

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["knowledge:read"],
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Chat fetch SSE", () => {
  it("sends the persisted Last-Event-ID and advances only an accepted next event", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(
      responseWithFrames([sseFrame(5, "evt_5")]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const cursors: Array<{ id: string; sequence: number }> = [];

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: () => {
        controller.abort();
        return "accepted";
      },
      onCursor: (cursor) => cursors.push(cursor),
      onConnectionChange: () => undefined,
    });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("last-event-id")).toBe("cursor_4");
    expect(new Headers(init.headers).get("x-principal-id")).toBe("alice");
    expect(cursors).toEqual([{ id: "cursor_5", sequence: 5 }]);
  });

  it("skips a server replay before the durable sequence and does not move the cursor backwards", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([
          sseFrame(1, "old_evt"),
          sseFrame(5, "new_evt"),
        ]),
      ),
    );
    const seen: string[] = [];
    const cursors: Array<{ id: string; sequence: number }> = [];

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: (frame) => {
        if (!isQuarantineFrame(frame) && !isDegradedFrame(frame)) {
          seen.push(frame.envelope.event_id);
        }
        controller.abort();
        return "accepted";
      },
      onCursor: (cursor) => cursors.push(cursor),
      onConnectionChange: () => undefined,
    });

    expect(seen).toEqual(["new_evt"]);
    expect(cursors).toEqual([{ id: "cursor_5", sequence: 5 }]);
  });

  it("does not advance a cursor when the domain reducer rejects the frame", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithFrames([sseFrame(1, "evt_1")])));
    const cursors: Array<{ id: string; sequence: number }> = [];

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: null,
      signal: controller.signal,
      onFrame: () => {
        controller.abort();
        return "rejected";
      },
      onCursor: (cursor) => cursors.push(cursor),
      onConnectionChange: () => undefined,
    });

    expect(cursors).toEqual([]);
  });

  it("reconnects from the unchanged cursor when the durable sequence has a gap", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(responseWithFrames([sseFrame(6, "evt_6")]));
    vi.stubGlobal("fetch", fetchMock);
    const seen: string[] = [];
    const cursors: Array<{ id: string; sequence: number }> = [];
    const connections: Array<{ state: string; error?: string }> = [];

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: (frame) => {
        if (!isQuarantineFrame(frame) && !isDegradedFrame(frame)) {
          seen.push(frame.envelope.event_id);
        }
        return "accepted";
      },
      onCursor: (cursor) => cursors.push(cursor),
      onConnectionChange: (state, error) => {
        connections.push(error === undefined ? { state } : { state, error });
        if (state === "retrying") controller.abort();
      },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([]);
    expect(cursors).toEqual([]);
    expect(connections.at(-1)?.state).toBe("retrying");
    expect(connections.at(-1)?.error).toContain("期望 5，收到 6");
  });

  it("cancels the poisoned response body before reconnecting", async () => {
    const controller = new AbortController();
    let sourceCancelled = false;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(streamController) {
        streamController.enqueue(encoder.encode(sseFrame(6, "evt_6")));
      },
      cancel() {
        sourceCancelled = true;
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }),
      ),
    );

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: () => "accepted",
      onCursor: () => undefined,
      onConnectionChange: (state) => {
        if (state === "retrying") controller.abort();
      },
    });

    expect(sourceCancelled).toBe(true);
  });

  it("keeps increasing backoff when 200 responses close without progress", async () => {
    vi.useFakeTimers();
    const timerSpy = vi.spyOn(window, "setTimeout");
    vi.stubGlobal(
      "fetch",
      vi.fn()
        .mockResolvedValueOnce(responseWithFrames([]))
        .mockResolvedValueOnce(responseWithFrames([]))
        .mockResolvedValueOnce(new Response(null, { status: 404 })),
    );

    const stream = streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: null,
      signal: new AbortController().signal,
      onFrame: () => "accepted",
      onCursor: () => undefined,
      onConnectionChange: () => undefined,
    });
    await vi.runAllTimersAsync();
    await stream;

    expect(timerSpy.mock.calls.map((call) => call[1])).toEqual([750, 1_500]);
  });
});

describe("Quarantined positions", () => {
  it("steps over a position the server declared skipped and keeps reading", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(
      responseWithFrames([sseFrame(1, "evt_1"), quarantineFrame(2), sseFrame(3, "evt_3")]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.seen).toEqual(["evt_1", "evt_3"]);
    // The notice moved the cursor by exactly one, so a reconnect resumes after
    // the unreadable row rather than in front of it.
    expect(trace.cursors).toEqual([
      { id: "cursor_1", sequence: 1 },
      { id: "cursor_2", sequence: 2 },
      { id: "cursor_3", sequence: 3 },
    ]);
    expect(trace.errors.filter((error) => error.includes("序号不连续"))).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("hands the skipped position to the subscriber, not only to the cursor", async () => {
    // `_quarantine_frame` in `routes/events.py` argues for a separate frame by
    // naming this project's own web reducer as the client that would otherwise
    // "drop the notification but keep the events on both sides of the hole".
    // That was the behaviour here: the cursor moved past position 2 and nothing
    // downstream ever learned it existed.
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([sseFrame(1, "evt_1"), quarantineFrame(2), sseFrame(3, "evt_3")]),
      ),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.disclosed).toEqual([2]);
    // And it arrives as a position, never as an event: the two events on either
    // side are still the only two events.
    expect(trace.seen).toEqual(["evt_1", "evt_3"]);
  });

  it("does not pass a position the subscriber refused", async () => {
    // Advancing over a hole the subscriber declined to record is the one
    // outcome that loses it for good -- the row would be behind the cursor and
    // absent from the page.
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([sseFrame(1, "evt_1"), quarantineFrame(2), sseFrame(3, "evt_3")]),
      ),
    );
    const trace = traceOf(controller, { rejectQuarantine: true });

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.disclosed).toEqual([2]);
    expect(trace.cursors).toEqual([{ id: "cursor_1", sequence: 1 }]);
    expect(trace.errors.at(-1)).toContain("未通过本地安全校验");
  });

  it("control: an unannounced hole still reconnects from the last safe cursor", async () => {
    // The load-bearing control. Same shape as the case above with the notice
    // removed: if this ever passes, the continuity check has been dismantled
    // and a silently shortened stream would go unnoticed.
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(
      responseWithFrames([sseFrame(1, "evt_1"), sseFrame(3, "evt_3")]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.seen).toEqual(["evt_1"]);
    expect(trace.cursors).toEqual([{ id: "cursor_1", sequence: 1 }]);
    expect(trace.errors.at(-1)).toContain("期望 2，收到 3");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("control: a stream with nothing quarantined is unchanged", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([sseFrame(1, "evt_1"), sseFrame(2, "evt_2"), sseFrame(3, "evt_3")]),
      ),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.seen).toEqual(["evt_1", "evt_2", "evt_3"]);
    expect(trace.cursors).toEqual([
      { id: "cursor_1", sequence: 1 },
      { id: "cursor_2", sequence: 2 },
      { id: "cursor_3", sequence: 3 },
    ]);
    // Nothing to disclose, and so nothing disclosed. A page that announced a
    // hole in a complete stream would be its own defect.
    expect(trace.disclosed).toEqual([]);
    expect(trace.states).toEqual(["connecting", "connected", "retrying"]);
  });

  it("steps over a run of consecutive skipped positions", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([
          sseFrame(1, "evt_1"),
          quarantineFrame(2),
          quarantineFrame(3),
          sseFrame(4, "evt_4"),
        ]),
      ),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.seen).toEqual(["evt_1", "evt_4"]);
    expect(trace.cursors.at(-1)).toEqual({ id: "cursor_4", sequence: 4 });
    expect(trace.errors.filter((error) => error.includes("序号不连续"))).toEqual([]);
  });

  it("advances past a page that was quarantined end to end", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const resumeFrom: Array<string | null> = [];
    const fetchMock = vi.fn((_url: string, init: RequestInit) => {
      resumeFrom.push(new Headers(init.headers).get("last-event-id"));
      if (resumeFrom.length >= 2) controller.abort();
      return Promise.resolve(
        responseWithFrames(
          resumeFrom.length === 1
            ? [quarantineFrame(1), quarantineFrame(2), quarantineFrame(3)]
            : [],
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const trace = traceOf(controller, { abortOnRetry: false });

    const stream = streamSession({
      eventsPath: "/v1/chat/sessions",
      ...trace.options,
      initialCursor: null,
      signal: controller.signal,
    });
    await vi.runAllTimersAsync();
    await stream;

    expect(trace.seen).toEqual([]);
    // Nothing was delivered, and all three positions were named. A page with no
    // steps at all can still say why it has none.
    expect(trace.disclosed).toEqual([1, 2, 3]);
    // Nothing was delivered, yet the second attempt resumes after the poison
    // instead of meeting it again -- the loop this frame exists to break.
    expect(resumeFrom).toEqual([null, "cursor_3"]);
    expect(trace.cursors.at(-1)).toEqual({ id: "cursor_3", sequence: 3 });
  });

  it("control: a notice for a position other than the next one still reconnects", async () => {
    // A notice is not a token that permits skipping ahead: it accounts for its
    // own position and nothing else, so position 2 vanishing here is a hole.
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(responseWithFrames([sseFrame(1, "evt_1"), quarantineFrame(3)])),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.cursors).toEqual([{ id: "cursor_1", sequence: 1 }]);
    expect(trace.errors.at(-1)).toContain("期望 2，收到 3");
  });

  it("control: a notice too malformed to parse leaves the gap unexplained", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([
          sseFrame(1, "evt_1"),
          quarantineFrame(2, { sequence: "2" }),
          sseFrame(3, "evt_3"),
        ]),
      ),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.seen).toEqual(["evt_1"]);
    expect(trace.errors.at(-1)).toContain("期望 2，收到 3");
  });

  it("does not trust a notice announcing another stream's position", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        responseWithFrames([sseFrame(1, "evt_1"), quarantineFrame(2, { stream_id: "ses_other" })]),
      ),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions", ...trace.options, initialCursor: null, signal: controller.signal });

    expect(trace.cursors).toEqual([{ id: "cursor_1", sequence: 1 }]);
    expect(trace.errors.at(-1)).toContain("不可信的持久事件");
  });

  it("ignores a replayed notice for a position the cursor already passed", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(responseWithFrames([quarantineFrame(2), sseFrame(5, "evt_5")])),
    );
    const trace = traceOf(controller);

    await streamSession({
      eventsPath: "/v1/chat/sessions",
      ...trace.options,
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
    });

    expect(trace.seen).toEqual(["evt_5"]);
    expect(trace.cursors).toEqual([{ id: "cursor_5", sequence: 5 }]);
  });
});

interface StreamTrace {
  seen: string[];
  // What the subscriber was told about positions rather than events. Separate
  // from `seen` on purpose: a notice is not an event, and a trace that mixed
  // the two could not tell a delivered step from a declared hole.
  disclosed: number[];
  cursors: Array<{ id: string; sequence: number }>;
  states: string[];
  errors: string[];
  options: {
    identity: PrincipalIdentity;
    sessionId: string;
    onFrame: (frame: SseChunkFrame) => FrameAcceptance;
    onCursor: (cursor: { id: string; sequence: number }) => void;
    onConnectionChange: (state: string, error?: string) => void;
  };
}

function traceOf(
  controller: AbortController,
  {
    abortOnRetry = true,
    rejectQuarantine = false,
  }: { abortOnRetry?: boolean; rejectQuarantine?: boolean } = {},
): StreamTrace {
  const trace: StreamTrace = {
    seen: [],
    disclosed: [],
    cursors: [],
    states: [],
    errors: [],
    options: {
      identity: IDENTITY,
      sessionId: "ses_1",
      onFrame: (frame) => {
        if (isQuarantineFrame(frame)) {
          trace.disclosed.push(frame.quarantined.sequence);
          return rejectQuarantine ? "rejected" : "accepted";
        }
        if (isDegradedFrame(frame)) return "accepted";
        trace.seen.push(frame.envelope.event_id);
        return "accepted";
      },
      onCursor: (cursor) => trace.cursors.push(cursor),
      onConnectionChange: (state, error) => {
        trace.states.push(state);
        if (error !== undefined) trace.errors.push(error);
        if (abortOnRetry && state === "retrying") controller.abort();
      },
    },
  };
  return trace;
}

function quarantineFrame(sequence: number, overrides: Record<string, unknown> = {}): string {
  const notice = {
    event_id: `evt_${sequence}`,
    event_type: "ModelCompleted",
    schema_version: 1,
    sequence,
    stream_id: "ses_1",
    ...overrides,
  };
  return `id: cursor_${sequence}\nevent: stream.quarantined\ndata: ${JSON.stringify(notice)}\n\n`;
}

function responseWithFrames(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frames.join("")));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function sseFrame(sequence: number, eventId: string): string {
  const envelope = {
    schema_version: 1,
    event_id: eventId,
    stream_id: "ses_1",
    run_id: "run_1",
    event_type: "RunStarted",
    durability: "durable",
    timestamp: "2026-08-02T12:00:00Z",
    payload: {
      kind: "RunStarted",
      run_kind: "chat",
      model_profile: "main",
      tool_names: [],
      budget: { max_steps: 4, max_tool_calls: 4 },
    },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return `id: cursor_${sequence}\nevent: RunStarted\ndata: ${JSON.stringify(envelope)}\n\n`;
}
