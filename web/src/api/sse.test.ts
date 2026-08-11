import { describe, expect, it } from "vitest";
import type { ParsedChunk, SseFrame } from "./sse";
import {
  QUARANTINE_EVENT,
  isEventEnvelope,
  isQuarantineFrame,
  isQuarantineNotice,
  parseSseChunk,
} from "./sse";

const ENVELOPE = {
  schema_version: 1,
  event_id: "evt_1",
  stream_id: "session_1",
  run_id: "run_1",
  event_type: "AnswerCommitted",
  durability: "durable",
  timestamp: "2026-08-02T00:00:00Z",
  payload: { kind: "AnswerCommitted", text: "safe", citations: [] },
  sequence: 1,
  task_id: null,
  graph_node_id: null,
  parent_event_id: null,
} as const;

describe("parseSseChunk", () => {
  it("keeps a split frame until its delimiter arrives", () => {
    const first = parseSseChunk(`id: session_1:1\nevent: AnswerCommitted\ndata: ${JSON.stringify(ENVELOPE)}`);
    expect(first.frames).toEqual([]);
    expect(first.remainder).not.toBe("");

    const second = parseSseChunk(`${first.remainder}\n\n`);
    expect(second.frames).toHaveLength(1);
    expect(second.frames[0]?.id).toBe("session_1:1");
    expect(eventFrame(second).envelope.payload.kind).toBe("AnswerCommitted");
  });

  it("ignores heartbeat comments and invalid JSON", () => {
    const parsed = parseSseChunk(": heartbeat\n\ndata: not-json\n\n");
    expect(parsed.frames).toEqual([]);
  });

  it("accepts CRLF and joins multi-line data", () => {
    const json = JSON.stringify(ENVELOPE);
    const splitAt = json.indexOf(',"event_type"');
    const parsed = parseSseChunk(
      `id: session_1:1\r\nevent: AnswerCommitted\r\ndata: ${json.slice(0, splitAt)}\r\ndata: ${json.slice(splitAt)}\r\n\r\n`,
    );
    expect(eventFrame(parsed).envelope.event_id).toBe("evt_1");
  });

  it("preserves a CRLF pair split across fetch chunks", () => {
    const wire =
      `id: session_1:1\r\nevent: AnswerCommitted\r\n` +
      `data: ${JSON.stringify(ENVELOPE)}\r\n\r\n`;
    const splitAt = wire.indexOf("\r\n") + 1;
    const first = parseSseChunk(wire.slice(0, splitAt));
    const second = parseSseChunk(first.remainder + wire.slice(splitAt));

    expect(first.frames).toEqual([]);
    expect(first.remainder.endsWith("\r")).toBe(true);
    expect(second.frames).toHaveLength(1);
    expect(second.frames[0]?.id).toBe("session_1:1");
  });

  it("rejects a frame whose announced event and payload disagree", () => {
    const parsed = parseSseChunk(
      `event: ModelCompleted\ndata: ${JSON.stringify(ENVELOPE)}\n\n`,
    );
    expect(parsed.frames).toEqual([]);
  });
});

describe("isEventEnvelope", () => {
  it("requires a durable sequence and matching event kind", () => {
    expect(isEventEnvelope(ENVELOPE, "AnswerCommitted")).toBe(true);
    expect(isEventEnvelope({ ...ENVELOPE, durability: "transient" })).toBe(false);
    expect(isEventEnvelope({ ...ENVELOPE, sequence: null })).toBe(false);
    expect(
      isEventEnvelope({
        ...ENVELOPE,
        payload: { ...ENVELOPE.payload, kind: "ModelCompleted" },
      }),
    ).toBe(false);
  });
});

const NOTICE = {
  event_id: "evt_2",
  event_type: "ModelCompleted",
  schema_version: 1,
  sequence: 2,
  stream_id: "session_1",
} as const;

describe("quarantine frames", () => {
  it("parses a skipped position as its own kind of frame, never as an envelope", () => {
    const parsed = parseSseChunk(quarantineWire(NOTICE));

    expect(parsed.frames).toHaveLength(1);
    const frame = parsed.frames[0];
    expect(frame).toBeDefined();
    if (frame === undefined || !isQuarantineFrame(frame)) throw new Error("expected a notice");
    expect(frame.id).toBe("session_1:2");
    expect(frame.event).toBe(QUARANTINE_EVENT);
    expect(frame.quarantined).toEqual(NOTICE);
    expect("envelope" in frame).toBe(false);
  });

  it("keeps a notice in stream order between the events around it", () => {
    const before = { ...ENVELOPE, sequence: 1 };
    const after = { ...ENVELOPE, event_id: "evt_3", sequence: 3 };
    const parsed = parseSseChunk(
      `id: session_1:1\nevent: AnswerCommitted\ndata: ${JSON.stringify(before)}\n\n` +
        quarantineWire(NOTICE) +
        `id: session_1:3\nevent: AnswerCommitted\ndata: ${JSON.stringify(after)}\n\n`,
    );

    expect(parsed.frames.map((frame) => (isQuarantineFrame(frame) ? "skipped" : frame.envelope.event_id))).toEqual([
      "evt_1",
      "skipped",
      "evt_3",
    ]);
  });

  it("drops a notice that cannot name a position, leaving the gap unexplained", () => {
    // Each of these is a frame the stream must NOT treat as permission to skip:
    // dropping it turns the hole back into an unannounced gap, which the
    // reducer's continuity check is there to catch.
    for (const broken of [
      { ...NOTICE, sequence: "2" },
      { ...NOTICE, sequence: 0 },
      { ...NOTICE, sequence: 2.5 },
      { ...NOTICE, stream_id: "" },
      { ...NOTICE, event_id: "" },
      { ...NOTICE, schema_version: 2 },
    ]) {
      expect(parseSseChunk(quarantineWire(broken)).frames).toEqual([]);
    }
  });

  it("keeps a notice whose damaged row has no readable type", () => {
    // The type is copied from a row the server could not decode; an empty one
    // describes the damage and says nothing about the position's validity.
    const parsed = parseSseChunk(quarantineWire({ ...NOTICE, event_type: "" }));
    expect(parsed.frames).toHaveLength(1);
  });
});

describe("isQuarantineNotice", () => {
  it("requires a schema, both ids, and a safe position", () => {
    expect(isQuarantineNotice(NOTICE)).toBe(true);
    expect(isQuarantineNotice({ ...NOTICE, sequence: Number.MAX_SAFE_INTEGER + 2 })).toBe(false);
    expect(isQuarantineNotice({ ...NOTICE, event_type: 7 })).toBe(false);
    expect(isQuarantineNotice(null)).toBe(false);
    expect(isQuarantineNotice("stream.quarantined")).toBe(false);
  });
});

/** The chunk's first frame, insisting it carried an envelope. */
function eventFrame(parsed: ParsedChunk): SseFrame {
  const frame = parsed.frames[0];
  if (frame === undefined || isQuarantineFrame(frame)) {
    throw new Error("expected an event frame");
  }
  return frame;
}

function quarantineWire(notice: Record<string, unknown>): string {
  return `id: session_1:${String(notice.sequence)}\nevent: ${QUARANTINE_EVENT}\ndata: ${JSON.stringify(notice)}\n\n`;
}
