import type { EventEnvelope } from "./types";

export interface SseFrame {
  id: string | null;
  event: string;
  envelope: EventEnvelope;
}

export interface ParsedChunk {
  frames: SseFrame[];
  remainder: string;
}

export function parseSseChunk(source: string, flush = false): ParsedChunk {
  // A fetch chunk may end between the two bytes of CRLF. Preserve that lone
  // CR until the next chunk arrives; normalizing it immediately would turn the
  // following LF into a second newline and manufacture an SSE frame boundary.
  const carriesCarriageReturn = !flush && source.endsWith("\r");
  const stable = carriesCarriageReturn ? source.slice(0, -1) : source;
  const normalized =
    stable.replaceAll("\r\n", "\n").replaceAll("\r", "\n") +
    (carriesCarriageReturn ? "\r" : "");
  const parts = normalized.split("\n\n");
  const remainder = flush ? "" : (parts.pop() ?? "");
  const frames: SseFrame[] = [];

  for (const raw of parts) {
    const frame = parseFrame(raw);
    if (frame !== null) frames.push(frame);
  }
  return { frames, remainder };
}

function parseFrame(raw: string): SseFrame | null {
  let id: string | null = null;
  let event = "message";
  const data: string[] = [];

  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).trimStart();
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }

  if (data.length === 0) return null;
  try {
    const candidate: unknown = JSON.parse(data.join("\n"));
    if (!isEventEnvelope(candidate, event)) return null;
    return {
      id,
      event,
      envelope: candidate,
    };
  } catch {
    return null;
  }
}

/**
 * Validate only the fields the stream reducer relies on. Unknown payload
 * fields remain forward-compatible, while malformed frames can never advance
 * the durable replay cursor.
 */
export function isEventEnvelope(
  value: unknown,
  announcedEvent?: string,
): value is EventEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const envelope = value as Record<string, unknown>;
  const payload = envelope.payload;
  if (typeof payload !== "object" || payload === null) return false;
  const kind = (payload as Record<string, unknown>).kind;
  if (
    envelope.schema_version !== 1 ||
    typeof envelope.event_id !== "string" ||
    envelope.event_id.length === 0 ||
    typeof envelope.stream_id !== "string" ||
    envelope.stream_id.length === 0 ||
    typeof envelope.run_id !== "string" ||
    envelope.run_id.length === 0 ||
    typeof envelope.event_type !== "string" ||
    typeof kind !== "string" ||
    envelope.event_type !== kind ||
    (announcedEvent !== undefined && announcedEvent !== envelope.event_type) ||
    envelope.durability !== "durable" ||
    typeof envelope.sequence !== "number" ||
    !Number.isSafeInteger(envelope.sequence) ||
    envelope.sequence < 1
  ) {
    return false;
  }
  return true;
}
