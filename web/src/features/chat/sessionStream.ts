import { identityHeaders } from "../../api/client";
import {
  isDegradedFrame,
  isLiveFrame,
  isQuarantineFrame,
  parseSseChunk,
  type SseChunkFrame,
  type SseLiveFrame,
  type SseQuarantineFrame,
} from "../../api/sse";
import type { PrincipalIdentity } from "../../api/types";
import type { ChatConnectionState } from "./model";
import type { StoredChatCursor } from "./storage";

export type FrameAcceptance = "accepted" | "duplicate" | "rejected";

interface SessionStreamOptions {
  identity: PrincipalIdentity;
  sessionId: string;
  initialCursor: StoredChatCursor | null;
  signal: AbortSignal;
  // Both kinds of frame, because both are things the reader is owed: the event
  // that happened, and the position that could not be delivered. The subscriber
  // decides what to do with each; this file's job is to hand them over in the
  // order they arrived.
  onFrame: (frame: SseChunkFrame) => FrameAcceptance;
  onCursor: (cursor: StoredChatCursor) => void;
  onConnectionChange: (state: ChatConnectionState, error?: string) => void;
  //: How many live events the server dropped before this reader took them.
  //: Optional because it is not history: a subscriber that only renders the
  //: durable record has nothing to do with it, and should not be forced to say
  //: so with an empty function.
  onLiveGap?: (dropped: number) => void;
}

class PermanentStreamError extends Error {}
class RecoverableStreamError extends Error {}

export async function streamChatSession(options: SessionStreamOptions): Promise<void> {
  let cursor = options.initialCursor;
  let retryMilliseconds = 750;
  options.onConnectionChange("connecting");

  while (!options.signal.aborted) {
    try {
      const headers: Record<string, string> = {
        accept: "text/event-stream",
        ...identityHeaders(options.identity),
      };
      if (cursor !== null) headers["last-event-id"] = cursor.id;

      const response = await fetch(
        `/v1/chat/sessions/${encodeURIComponent(options.sessionId)}/events`,
        { headers, signal: options.signal },
      );
      if (!response.ok || response.body === null) {
        const message = `事件流不可用（HTTP ${response.status}）`;
        if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429) {
          throw new PermanentStreamError(message);
        }
        throw new Error(message);
      }

      options.onConnectionChange("connected");
      const reader = response.body.getReader();
      try {
        const decoder = new TextDecoder();
        let buffer = "";
        const accept = (frame: SseChunkFrame) => {
          const previous = cursor;
          cursor = acceptFrame(options, frame, cursor);
          // A 200 alone is not progress: an immediately closed or poison
          // stream must keep backing off. A durable accepted position proves
          // this connection made forward progress and may reset the retry.
          if (cursor !== previous) retryMilliseconds = 750;
        };

        while (!options.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) {
            buffer += decoder.decode();
            const flushed = parseSseChunk(buffer, true);
            flushed.frames.forEach(accept);
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSseChunk(buffer);
          buffer = parsed.remainder;
          parsed.frames.forEach(accept);
        }
      } finally {
        // A reducer rejection or sequence gap exits by exception while the
        // HTTP response may still be infinite. Explicit cancellation prevents
        // each retry from leaving another live subscription behind.
        try {
          await reader.cancel();
        } catch {
          // Abort/network errors already closed the source.
        }
        reader.releaseLock();
      }

      if (options.signal.aborted) return;
      options.onConnectionChange("retrying", "事件流已断开，正在从上次游标重连");
      await abortableDelay(retryMilliseconds, options.signal);
      retryMilliseconds = Math.min(retryMilliseconds * 2, 5_000);
    } catch (error) {
      if (options.signal.aborted) return;
      const message = error instanceof Error ? error.message : "事件流连接失败";
      if (error instanceof PermanentStreamError) {
        options.onConnectionChange("unavailable", message);
        return;
      }
      options.onConnectionChange("retrying", message);
      await abortableDelay(retryMilliseconds, options.signal);
      retryMilliseconds = Math.min(retryMilliseconds * 2, 5_000);
    }
  }
}

function acceptFrame(
  options: SessionStreamOptions,
  frame: SseChunkFrame,
  current: StoredChatCursor | null,
): StoredChatCursor | null {
  if (isQuarantineFrame(frame)) return acceptQuarantine(options, frame, current);
  if (isDegradedFrame(frame)) {
    // Nothing to apply and nothing to move. The gap is in the live view only;
    // the durable history behind it is complete and still arriving.
    options.onLiveGap?.(frame.degraded.dropped_events);
    return current;
  }
  if (isLiveFrame(frame)) return acceptLive(options, frame, current);

  const sequence = frame.envelope.sequence;
  if (
    frame.id === null ||
    !frame.id ||
    frame.envelope.stream_id !== options.sessionId ||
    frame.envelope.durability !== "durable" ||
    sequence === null ||
    !Number.isInteger(sequence) ||
    sequence < 1
  ) {
    throw new RecoverableStreamError("事件流返回了不可信的持久事件，正在从安全游标重连");
  }

  // If a cursor became incompatible across a deploy, the server deliberately
  // replays from the beginning. The persisted durable sequence prevents that
  // replay from rebuilding old steps or claiming a new pending turn.
  if (current !== null && sequence <= current.sequence) return current;

  requireNextPosition(sequence, current);
  const acceptance = options.onFrame(frame);
  if (acceptance === "rejected") {
    throw new RecoverableStreamError("事件未通过本地安全校验，正在从安全游标重连");
  }

  const next = { id: frame.id, sequence };
  options.onCursor(next);
  return next;
}

/**
 * An event that is happening rather than one that was recorded.
 *
 * Three things this deliberately does *not* do, each of which would be correct
 * for a durable frame and wrong here:
 *
 * * it does not advance the cursor. There is no position to advance to, and
 *   writing one would point a reconnect at a place the replay cannot serve;
 * * it does not check `frame.id`. The parser already proved it is absent, and
 *   that absence is the frame's defining property rather than a defect;
 * * it does not reconnect when the reducer refuses the event. A live event is
 *   an accelerator: dropping one costs a moment of stale text, while tearing
 *   down the connection would cost the durable replay riding on it.
 *
 * The one check that stays is ownership. A frame for another stream on this
 * socket is not something to render, whatever it is.
 */
function acceptLive(
  options: SessionStreamOptions,
  frame: SseLiveFrame,
  current: StoredChatCursor | null,
): StoredChatCursor | null {
  if (frame.envelope.stream_id === options.sessionId) options.onFrame(frame);
  return current;
}

/**
 * A position the server says it examined and could not deliver.
 *
 * No event reaches the reducer -- there is nothing to apply, and the local
 * history is short by exactly this one position. Two things happen instead. The
 * cursor moves past it, which is the frame's stated purpose: its id *is* the
 * skipped position, so a reconnect resumes after the unreadable row instead of
 * arriving in front of it again on every attempt. And the position itself is
 * handed over, because a subscriber that only advanced would keep the events on
 * both sides of the hole and never be able to say the hole was there.
 */
function acceptQuarantine(
  options: SessionStreamOptions,
  frame: SseQuarantineFrame,
  current: StoredChatCursor | null,
): StoredChatCursor | null {
  // The parser already proved the notice's own shape -- schema, ids, a safe
  // sequence of at least 1. Checked here is only what a parser cannot know:
  // that the frame carried a cursor to resume from, and that it describes this
  // subscription rather than some other stream.
  const { sequence } = frame.quarantined;
  if (frame.id === null || !frame.id || frame.quarantined.stream_id !== options.sessionId) {
    throw new RecoverableStreamError("事件流返回了不可信的持久事件，正在从安全游标重连");
  }

  // A replay from the beginning re-announces holes already passed, for the
  // same reason it re-sends events already applied.
  if (current !== null && sequence <= current.sequence) return current;

  requireNextPosition(sequence, current);
  // Same order as an event: the subscriber sees the frame before the cursor
  // moves, so a refusal leaves the position unpassed rather than passed and
  // undisclosed.
  if (options.onFrame(frame) === "rejected") {
    throw new RecoverableStreamError("事件未通过本地安全校验，正在从安全游标重连");
  }
  const next = { id: frame.id, sequence };
  options.onCursor(next);
  return next;
}

/**
 * Insist that a frame occupy the very next position, or reconnect.
 *
 * This check is what keeps the history from quietly getting shorter. A
 * subscriber cannot tell a hole from an event that has not been written yet,
 * so an unexplained jump has to be read as a lost position and re-fetched from
 * the last cursor known to be good.
 *
 * A quarantine frame is not an exemption from that rule -- it satisfies it.
 * The notice occupies `expected` itself and moves the cursor by exactly one,
 * the same as an event does, so the only positions ever passed over are the
 * ones the server explicitly declared as skipped. A gap nobody announced still
 * throws here, and that is deliberate: loosening this into "skip ahead to
 * whatever arrived" would trade the poison-row reconnect loop for silent data
 * loss, which is the failure this check exists to make impossible.
 */
function requireNextPosition(sequence: number, current: StoredChatCursor | null): void {
  const expected = (current?.sequence ?? 0) + 1;
  if (sequence !== expected) {
    throw new RecoverableStreamError(
      `事件流序号不连续（期望 ${expected}，收到 ${sequence}），正在从安全游标重连`,
    );
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve();
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
