import type { EventEnvelope } from "./types";

/**
 * The SSE event name for a durable position the server examined and did not
 * deliver. Dotted and lower-case, unlike every domain event type -- those are
 * payload class names, `RunStarted` and friends -- so dispatching on `event:`
 * can never confuse this with something a workflow emitted.
 */
export const QUARANTINE_EVENT = "stream.quarantined";

/**
 * The SSE event name for live events the server had to drop before this reader
 * took them. Same naming rule as above, and the same reason: a client that
 * dispatches on `event:` has to be able to tell "you missed some live text"
 * from anything a run emitted.
 */
export const DEGRADED_EVENT = "stream.degraded";

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

/**
 * A frame carrying one live event: something happening now, with no position.
 *
 * Its own type rather than an `SseFrame` with a null id, and for the same
 * reason the quarantine notice has its own: the two are handled by different
 * code with different rules, and the type system is what stops one from being
 * fed to the other. `safeEventFromFrame` -- the only thing that turns a frame
 * into durable history -- takes `SseFrame` and only `SseFrame`, so a live event
 * cannot reach the replay cursor even by accident.
 *
 * `id` is `null` by construction, not by convention. A transient event has no
 * sequence to resume from, so the server sends no `id:` line; per the SSE
 * specification that leaves `Last-Event-ID` untouched, which is what lets these
 * share a connection with the replay without disturbing it.
 */
export interface SseLiveFrame {
  id: null;
  event: string;
  envelope: EventEnvelope;
}

/** What the server says about live events it could not hand to this reader. */
export interface DegradedNotice {
  dropped_events: number;
}

/**
 * A frame that declares the live view has a gap -- and that the history does not.
 *
 * Deliberately carries no id. The quarantine notice carries one because it
 * names a durable position a reconnect must resume *after*; nothing dropped
 * here was ever addressable, so there is no position to move to and nothing
 * for a client to go and fetch. The only honest report is that the live text is
 * no longer complete.
 */
export interface SseDegradedFrame {
  id: null;
  event: typeof DEGRADED_EVENT;
  degraded: DegradedNotice;
}

/** Anything a well-formed frame on this stream can be. */
export type SseChunkFrame =
  | SseFrame
  | SseQuarantineFrame
  | SseLiveFrame
  | SseDegradedFrame;

export function isQuarantineFrame(frame: SseChunkFrame): frame is SseQuarantineFrame {
  return "quarantined" in frame;
}

export function isDegradedFrame(frame: SseChunkFrame): frame is SseDegradedFrame {
  return "degraded" in frame;
}

/**
 * Whether this frame describes something happening rather than something
 * recorded. Decided on the id, because that is the property the whole
 * arrangement rests on: no id means no position means nothing to resume from.
 */
export function isLiveFrame(frame: SseChunkFrame): frame is SseLiveFrame {
  return "envelope" in frame && frame.id === null;
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
    if (event === DEGRADED_EVENT) {
      // An id here would be the server claiming a position for something that
      // never had one, so a notice carrying one is malformed rather than
      // generous. Dropping it is the safe reading: the live view then looks
      // complete when it is not, which is visible, rather than the cursor
      // moving to a place replay cannot serve, which is not.
      if (id !== null || !isDegradedNotice(candidate)) return null;
      return { id: null, event: DEGRADED_EVENT, degraded: candidate };
    }
    // The id is what selects durable from live, and the check below is what
    // makes that selection a two-way equivalence rather than a convention: a
    // frame with an id must be durable and carry a position, and one without
    // must be transient and carry none. Either half alone would let a
    // malformed frame be read as the other kind.
    if (id === null) {
      if (!isEnvelopeShape(candidate, event, "transient")) return null;
      return { id: null, event, envelope: candidate };
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
  return isEnvelopeShape(value, announcedEvent, "durable");
}

/**
 * One envelope check for both kinds, differing only where they genuinely
 * differ: durability, and whether a position is present or absent.
 *
 * Shared rather than written twice, because the two lists have to stay
 * identical in every other respect and "somebody added a field to one of them"
 * is exactly how a transient frame would end up held to a weaker standard than
 * a durable one -- on the side that never passes through the log.
 */
function isEnvelopeShape(
  value: unknown,
  announcedEvent: string | undefined,
  durability: "durable" | "transient",
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
    envelope.durability !== durability
  ) {
    return false;
  }
  if (durability === "transient") {
    // Null, not merely absent: the server writes the field, and an envelope
    // that omitted it would be one this client cannot recognise as either kind.
    return envelope.sequence === null;
  }
  return (
    typeof envelope.sequence === "number" &&
    Number.isSafeInteger(envelope.sequence) &&
    envelope.sequence >= 1
  );
}

/** The one field a degraded notice carries, checked before it is believed. */
export function isDegradedNotice(value: unknown): value is DegradedNotice {
  if (typeof value !== "object" || value === null) return false;
  const notice = value as Record<string, unknown>;
  return (
    typeof notice.dropped_events === "number" &&
    Number.isSafeInteger(notice.dropped_events) &&
    notice.dropped_events >= 1
  );
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
