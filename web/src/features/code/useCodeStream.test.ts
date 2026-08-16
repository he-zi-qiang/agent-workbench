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
      expect(result.current.map((event) => event.event_id)).toEqual([
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
      expect(result.current.map((event) => event.event_id)).toEqual(["evt_2"]);
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
      expect(result.current).toHaveLength(1);
    });

    // The regression this exists for: a re-render that is not a change of
    // session must not disturb the list. It used to be emptied whenever the
    // turn stopped running, which destroyed the steps of the turn whose report
    // the reader was at that moment reading.
    rerender({ session: SESSION });

    expect(result.current).toHaveLength(1);
  });

  it("starts from empty and replays from the beginning on another session", async () => {
    const fetchMock = stubStream([durable(1, "evt_1")], { reusable: true });

    const { result, rerender } = renderHook(
      ({ session }) => useCodeStream(IDENTITY, session),
      { initialProps: { session: SESSION } },
    );
    await waitFor(() => {
      expect(result.current).toHaveLength(1);
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
