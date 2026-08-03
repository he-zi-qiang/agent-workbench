import { identityHeaders } from "../../api/client";
import { parseSseChunk, type SseFrame } from "../../api/sse";
import type { PrincipalIdentity } from "../../api/types";
import type { ChatConnectionState } from "./model";
import type { StoredChatCursor } from "./storage";

export type FrameAcceptance = "accepted" | "duplicate" | "rejected";

interface SessionStreamOptions {
  identity: PrincipalIdentity;
  sessionId: string;
  initialCursor: StoredChatCursor | null;
  signal: AbortSignal;
  onFrame: (frame: SseFrame) => FrameAcceptance;
  onCursor: (cursor: StoredChatCursor) => void;
  onConnectionChange: (state: ChatConnectionState, error?: string) => void;
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
        const accept = (frame: SseFrame) => {
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
  frame: SseFrame,
  current: StoredChatCursor | null,
): StoredChatCursor | null {
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

  const expected = (current?.sequence ?? 0) + 1;
  if (sequence !== expected) {
    throw new RecoverableStreamError(
      `事件流序号不连续（期望 ${expected}，收到 ${sequence}），正在从安全游标重连`,
    );
  }
  const acceptance = options.onFrame(frame);
  if (acceptance === "rejected") {
    throw new RecoverableStreamError("事件未通过本地安全校验，正在从安全游标重连");
  }

  const next = { id: frame.id, sequence };
  options.onCursor(next);
  return next;
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
