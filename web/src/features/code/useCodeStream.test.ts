/**
 * The step stream, driven through the real transport.
 *
 * The transport is not mocked here on purpose. What this hook gets wrong is
 * what it *returns* to the transport, and that is only visible when something
 * acts on the return value: an acceptance the transport reads as "this frame
 * failed validation" tears the connection down and resumes from a cursor that
 * has not moved, so the same frame arrives again and the stream stops making
 * progress. A test that called `onFrame` directly would see the same rows and
 * none of that.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrincipalIdentity } from "../../api/types";
import { useCodeStream } from "./useCodeStream";

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["workspace:write"],
};

const SESSION = "ses_code_1";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useCodeStream", () => {
  it("shows the steps a running turn emits, in the order they arrived", async () => {
    stubStream([durable(1, "evt_1"), durable(2, "evt_2")]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.steps.map((event) => event.event_id)).toEqual([
        "evt_1",
        "evt_2",
      ]);
    });
  });

  it("keeps going after a position the server could not decode", async () => {
    const fetchMock = stubStream([quarantine(1), durable(2, "evt_2")]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // The event after the hole is the assertion. Refusing the notice would
    // leave the cursor in front of it, and every reconnect would arrive at the
    // same undecodable position -- so nothing past it would ever be shown.
    await waitFor(() => {
      expect(result.current.steps.map((event) => event.event_id)).toEqual(["evt_2"]);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("opens a stream for an open session before any turn runs", async () => {
    const fetchMock = stubStream([durable(1, "evt_1")]);

    renderHook(() => useCodeStream(IDENTITY, SESSION));

    // The inverse of what this asserted while the list was per-turn. The
    // subscription is scoped to the session now, and it has to be: the history
    // of turns that already finished arrives through this same replay, so a
    // stream that waited for a turn to start could never show a finished one.
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
  });

  it("keeps a finished turn's steps instead of emptying them", async () => {
    stubStream([durable(1, "evt_1")]);

    const { result, rerender } = renderHook(
      ({ session }) => useCodeStream(IDENTITY, session),
      { initialProps: { session: SESSION } },
    );
    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });

    // The regression this exists for: a re-render that is not a change of
    // session must not disturb the list. It used to be emptied whenever the
    // turn stopped running, which destroyed the steps of the turn whose report
    // the reader was at that moment reading.
    rerender({ session: SESSION });

    expect(result.current.steps).toHaveLength(1);
  });

  it("starts from empty and replays from the beginning on another session", async () => {
    const fetchMock = stubStream([durable(1, "evt_1")], { reusable: true });

    const { result, rerender } = renderHook(
      ({ session }) => useCodeStream(IDENTITY, session),
      { initialProps: { session: SESSION } },
    );
    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });

    rerender({ session: "ses_code_2" });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    // Both halves matter. Carrying the list over would show one session's
    // steps under another's transcript; carrying the *cursor* over would ask
    // the new session to resume from a position that belongs to a different
    // stream, and skip everything before it.
    const headers = new Headers(
      (fetchMock.mock.calls[1]?.[1] as RequestInit).headers,
    );
    expect(headers.get("last-event-id")).toBeNull();
  });
});

describe("useCodeStream reasoning", () => {
  it("accumulates one model call's reasoning as it streams", async () => {
    stubStream([thinking("Read "), thinking("the file.")]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.thinking).toBe("Read the file.");
    });
    // Beside the steps, never in them: a live frame has no position, so a
    // reconnect could not resume from one.
    expect(result.current.steps).toHaveLength(0);
  });

  it("replaces the thought when a new model call starts one", async () => {
    // A turn is several calls with a tool round between them. Appending across
    // them would show reasoning the model has already acted on.
    stubStream([thinking("first", "mc_1"), thinking("second", "mc_2")]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.thinking).toBe("second");
    });
  });

  it("clears the thought when the call that had it completes", async () => {
    // The excerpt is in the step that just arrived, so leaving the live block
    // up would show the same reasoning twice -- once as "thinking now".
    stubStream([thinking("weighing options"), completed(1)]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });
    expect(result.current.thinking).toBe("");
  });

  it("never shows one session's thought over another session's transcript", async () => {
    // The reason the return value is derived rather than cleared: a thought in
    // flight when the reader switches sessions would otherwise sit above the
    // new session's transcript, describing work it has nothing to do with.
    stubStream([thinking("thinking about session one")], { reusable: true });

    const { result, rerender } = renderHook(
      ({ session }) => useCodeStream(IDENTITY, session),
      { initialProps: { session: SESSION } },
    );
    await waitFor(() => {
      expect(result.current.thinking).toBe("thinking about session one");
    });

    rerender({ session: "ses_code_2" });

    expect(result.current.thinking).toBe("");
  });

  it("keeps a thought that belongs to a call which has not finished", async () => {
    // The control for the test above: a completion for another call must not
    // clear the reasoning of the one still running.
    stubStream([thinking("still going", "mc_2"), completed(1, "mc_1")]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });
    expect(result.current.thinking).toBe("still going");
  });
  it("shows what a running tool call is doing, keyed by the call", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox", elapsed_ms: 0 }),
      progress("call_b", { message: "staging 2 input file(s)", elapsed_ms: 0 }),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // Two at once, because the runtime runs read tools in parallel. A single
    // slot would show whichever reported last under both rows.
    await waitFor(() => {
      expect(result.current.progress.get("call_a")?.lines).toEqual([
        "executing in the sandbox",
      ]);
    });
    expect(result.current.progress.get("call_b")?.lines).toEqual([
      "staging 2 input file(s)",
    ]);
  });

  it("lets a heartbeat move the clock without blanking the phase", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox" }),
      progress("call_a", { elapsed_ms: 5000 }),
      progress("call_a", { elapsed_ms: 10_000 }),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // A beat carries a clock and no message, because nothing new has happened.
    // Letting it clear the block would empty the card every few seconds.
    await waitFor(() => {
      expect(result.current.progress.get("call_a")?.elapsedMs).toBe(10_000);
    });
    expect(result.current.progress.get("call_a")?.lines).toEqual([
      "executing in the sandbox",
    ]);
  });

  it("grows the block as the script prints, keeping only the tail", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox" }),
      // One record, four lines: the container's tail reads a chunk of bytes
      // rather than a line, so several prints inside one poll interval arrive
      // together (ADR-069).
      progress("call_a", { message: "chunk 0\nchunk 1\n\nchunk 2\n" }),
      ...Array.from({ length: 7 }, (_, index) =>
        progress("call_a", { message: `chunk ${String(index + 3)}` }),
      ),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.progress.get("call_a")?.lines).toContain("chunk 9");
    });
    const lines = result.current.progress.get("call_a")?.lines ?? [];
    // A window, not a transcript: the complete streams are in the tool result.
    expect(lines).toHaveLength(8);
    expect(lines[lines.length - 1]).toBe("chunk 9");
    // The phase has scrolled off, and the blank line inside the multi-line
    // record never took a slot.
    expect(lines).not.toContain("executing in the sandbox");
    expect(lines).not.toContain("");
  });

  it("drops a call's progress the moment that call returns", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox", elapsed_ms: 5000 }),
      toolDone(1, "call_a"),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // Otherwise "executing in the sandbox · 已运行 5 秒" stays frozen under a
    // step whose outcome is already drawn -- the console asserting motion that
    // has stopped.
    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });
    expect(result.current.progress.has("call_a")).toBe(false);
  });

  it("drops it for a call that failed, too", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox" }),
      toolDone(1, "call_a", "ToolFailed"),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });
    expect(result.current.progress.has("call_a")).toBe(false);
  });

  it("clears every running call when the run itself ends", async () => {
    stubStream([
      progress("call_a", { message: "executing in the sandbox" }),
      terminal(1, "RunCancelled"),
    ]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // A tool killed by the run's cancellation gets neither ToolCompleted nor
    // ToolFailed, so the per-call rule above never fires for it. Without this
    // its line would outlive the run that produced it.
    await waitFor(() => {
      expect(result.current.steps).toHaveLength(1);
    });
    expect(result.current.progress.size).toBe(0);
  });

  it("keeps the live progress out of the step list", async () => {
    stubStream([durable(1, "evt_1"), progress("call_a", { elapsed_ms: 5000 })]);

    const { result } = renderHook(() => useCodeStream(IDENTITY, SESSION));

    // Same rule as the deltas: a frame with no position cannot be resumed
    // from, so a list containing one would differ depending on when the reader
    // connected.
    await waitFor(() => {
      expect(result.current.progress.get("call_a")?.elapsedMs).toBe(5000);
    });
    expect(result.current.steps.map((event) => event.event_id)).toEqual(["evt_1"]);
  });
});

function stubStream(frames: string[], options: { reusable?: boolean } = {}) {
  const encoder = new TextEncoder();
  // One body, then nothing. A second connection attempt is what a wedged
  // stream does, and it has to be distinguishable from the first -- except
  // where a test is about the second connection, which says so.
  const fetchMock = vi.fn().mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        if (fetchMock.mock.calls.length > 1 && options.reusable !== true) return;
        resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(encoder.encode(frames.join("")));
                controller.close();
              },
            }),
            { status: 200, headers: { "content-type": "text/event-stream" } },
          ),
        );
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function durable(sequence: number, eventId: string): string {
  const envelope = {
    schema_version: 1,
    event_id: eventId,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: "ToolStarted",
    durability: "durable",
    timestamp: "2026-08-14T12:00:00Z",
    payload: {
      kind: "ToolStarted",
      tool_name: "workspace_write",
      call_id: `call_${String(sequence)}`,
      argument_digest: "a".repeat(64),
    },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return `id: cursor_${String(sequence)}\nevent: ToolStarted\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function thinking(text: string, call = "mc_1"): string {
  const envelope = {
    schema_version: 1,
    event_id: `evt_live_${call}_${text}`,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: "ModelThinkingDelta",
    durability: "transient",
    timestamp: "2026-08-14T12:00:00Z",
    payload: { kind: "ModelThinkingDelta", model_call_id: call, text },
    sequence: null,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  // No `id:` line: a live frame has no position (ADR-051).
  return `event: ModelThinkingDelta\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function completed(sequence: number, call = "mc_1"): string {
  const envelope = {
    schema_version: 1,
    event_id: `evt_done_${String(sequence)}`,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: "ModelCompleted",
    durability: "durable",
    timestamp: "2026-08-14T12:00:00Z",
    payload: {
      kind: "ModelCompleted",
      model_call_id: call,
      finish_reason: "stop",
      text: "done",
      thinking_preview: "the excerpt",
    },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return `id: cursor_${String(sequence)}\nevent: ModelCompleted\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function progress(
  toolCall: string,
  fields: { message?: string; elapsed_ms?: number; percent?: number },
): string {
  const envelope = {
    schema_version: 1,
    event_id: `evt_progress_${toolCall}_${JSON.stringify(fields)}`,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: "ToolProgress",
    durability: "transient",
    timestamp: "2026-08-14T12:00:00Z",
    payload: {
      kind: "ToolProgress",
      tool_call_id: toolCall,
      message: fields.message ?? null,
      percent: fields.percent ?? null,
      elapsed_ms: fields.elapsed_ms ?? null,
    },
    sequence: null,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  // No `id:` line: like every other transient frame, it has no position.
  return `event: ToolProgress\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function toolDone(sequence: number, toolCall: string, type = "ToolCompleted"): string {
  const envelope = {
    schema_version: 1,
    event_id: `evt_tool_${String(sequence)}`,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: type,
    durability: "durable",
    timestamp: "2026-08-14T12:00:00Z",
    payload: {
      kind: type,
      tool_call_id: toolCall,
      tool_name: "sandbox_run",
      status: "ok",
    },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return `id: cursor_${String(sequence)}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function terminal(sequence: number, type: string): string {
  const envelope = {
    schema_version: 1,
    event_id: `evt_run_${String(sequence)}`,
    stream_id: SESSION,
    run_id: "run_1",
    event_type: type,
    durability: "durable",
    timestamp: "2026-08-14T12:00:00Z",
    payload: { kind: type, reason_code: "cancel_requested" },
    sequence,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
  return `id: cursor_${String(sequence)}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function quarantine(sequence: number): string {
  const notice = {
    event_id: `evt_${String(sequence)}`,
    event_type: "ModelCompleted",
    schema_version: 1,
    sequence,
    stream_id: SESSION,
  };
  return `id: cursor_${String(sequence)}\nevent: stream.quarantined\ndata: ${JSON.stringify(notice)}\n\n`;
}
