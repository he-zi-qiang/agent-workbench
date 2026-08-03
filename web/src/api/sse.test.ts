import { describe, expect, it } from "vitest";
import { isEventEnvelope, parseSseChunk } from "./sse";

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
    expect(second.frames[0]?.envelope.payload.kind).toBe("AnswerCommitted");
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
    expect(parsed.frames[0]?.envelope.event_id).toBe("evt_1");
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
