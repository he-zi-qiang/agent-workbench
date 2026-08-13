import type { EventEnvelope } from "./types";

/**
 * The SSE event name for a durable position the server examined and did not
 * deliver. Dotted and lower-case, unlike every domain event type -- those are
 * payload class names, `RunStarted` and friends -- so dispatching on `event:`
 * can never confuse this with something a workflow emitted.
 */
export const QUARANTINE_EVENT = "stream.quarantined";

/** A frame carrying one durable event. */
export interface SseFrame {
  id: string | null;
  event: string;
  envelope: EventEnvelope;
}

/**
 * What the server discloses about a position it skipped: which one, and the
 * little the damaged row still says about itself. There is no payload, no run,
 * and nothing to apply.
 */
export interface QuarantineNotice {
  event_id: string;
  event_type: string;
  schema_version: number;
  sequence: number;
  stream_id: string;
}

/**
 * A frame that declares a position was skipped rather than delivered.
 *
 * Kept as its own type instead of being widened into `SseFrame`: an envelope
 * is the log's record of one thing that happened, and forcing this into that
 * shape would mean inventing a payload for an event nobody could read.
 *
 * `reduceChatFrame` takes the union, so that showing the hole is possible at
 * all; what stops a notice from being mistaken for history is its first line,
 * which answers a quarantine frame in its own branch. Past that branch the
 * type system still holds the line: `safeEventFromFrame`, the only thing that
 * turns a frame into an event, takes `SseFrame` and only `SseFrame`.
 */
export interface SseQuarantineFrame {
  id: string | null;
  event: typeof QUARANTINE_EVENT;
  quarantined: QuarantineNotice;
}

/** Anything a well-formed frame on this stream can be. */
export type SseChunkFrame = SseFrame | SseQuarantineFrame;

export function isQuarantineFrame(frame: SseChunkFrame): frame is SseQuarantineFrame {
  return "quarantined" in frame;
}

export interface ParsedChunk {
  frames: SseChunkFrame[];
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
  const frames: SseChunkFrame[] = [];

  for (const raw of parts) {
    const frame = parseFrame(raw);
    if (frame !== null) frames.push(frame);
  }
  return { frames, remainder };
}

function parseFrame(raw: string): SseChunkFrame | null {
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
    // The announced name is what selects the shape, which is the whole reason
    // the server spends a separate name on this instead of a field inside the
    // envelope: a client that dispatches by event type reads the notice or
    // drops it, and cannot silently keep the events on either side of the hole.
    if (event === QUARANTINE_EVENT) {
      if (!isQuarantineNotice(candidate)) return null;
      return { id, event: QUARANTINE_EVENT, quarantined: candidate };
    }
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

/**
 * Validate a notice at least as strictly as an envelope, for a sharper reason:
 * this is the only frame that lets the stream pass over a position without an
 * event, so a malformed one has to be discarded rather than believed. Dropping
 * it turns the hole back into an unannounced gap, which is exactly what the
 * continuity check is there to catch -- the safe reading, and the behaviour
 * that existed before this frame did.
 */
export function isQuarantineNotice(value: unknown): value is QuarantineNotice {
  if (typeof value !== "object" || value === null) return false;
  const notice = value as Record<string, unknown>;
  return (
    notice.schema_version === 1 &&
    typeof notice.event_id === "string" &&
    notice.event_id.length > 0 &&
    typeof notice.stream_id === "string" &&
    notice.stream_id.length > 0 &&
    // Length is not required of the type: it is copied verbatim from a row
    // this server could not decode, so an empty one is a fact about the damage
    // rather than a reason to distrust the position being reported.
    typeof notice.event_type === "string" &&
    typeof notice.sequence === "number" &&
    Number.isSafeInteger(notice.sequence) &&
    notice.sequence >= 1
  );
}
