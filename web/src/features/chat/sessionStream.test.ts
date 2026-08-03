import type { PrincipalIdentity } from "../../api/types";
import { afterEach, describe, expect, it, vi } from "vitest";
import { streamChatSession } from "./sessionStream";

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

    await streamChatSession({
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

    await streamChatSession({
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: (frame) => {
        seen.push(frame.envelope.event_id);
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

    await streamChatSession({
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

    await streamChatSession({
      identity: IDENTITY,
      sessionId: "ses_1",
      initialCursor: { id: "cursor_4", sequence: 4 },
      signal: controller.signal,
      onFrame: (frame) => {
        seen.push(frame.envelope.event_id);
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

    await streamChatSession({
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

    const stream = streamChatSession({
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
