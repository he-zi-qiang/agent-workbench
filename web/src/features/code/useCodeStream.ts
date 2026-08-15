/**
 * Watching one coding session's steps while its turn runs.
 *
 * A coding turn holds its request open for as long as the agent works, which
 * can be minutes. Without this the page shows a spinner for all of it, and a
 * spinner cannot be told apart from a hang -- which is the same reason the
 * server bothers to emit steps at all.
 *
 * It reuses the reconnecting transport chat uses. That module used to live
 * inside the chat feature and had the chat path baked in; it now takes the
 * path, and this is the second caller that made that worth doing.
 *
 * Durable events only, deliberately. Live frames carry no position, so a
 * reconnect cannot resume from one; a subscriber that mixed them would show a
 * different list depending on when it connected. What is lost by ignoring them
 * is token-by-token text, which a coding session does not display anyway.
 */

import { useEffect, useRef, useState } from "react";
import { streamSession } from "../../api/sessionStream";
import {
  isDegradedFrame,
  isLiveFrame,
  isQuarantineFrame,
  type StreamCursor,
} from "../../api/sse";
import type { EventEnvelope, PrincipalIdentity } from "../../api/types";

export const CODE_EVENTS_PATH = "/v1/code/sessions";

/** How many steps to keep. A turn is bounded, but a session is not. */
const KEPT_EVENTS = 200;

export function useCodeStream(
  identity: PrincipalIdentity,
  sessionId: string | undefined,
  watching: boolean,
): EventEnvelope[] {
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  // The position survives the gap between turns; the list does not. Those are
  // different questions -- "what has this subscriber already been shown" and
  // "what is happening right now" -- and the first browser run made the cost of
  // conflating them visible: a second turn opened showing the first turn's
  // steps, because a watch that starts from no cursor is served the session's
  // whole history from sequence 1.
  const seen = useRef<StreamCursor | null>(null);

  useEffect(() => {
    if (sessionId === undefined || !watching) return;
    const controller = new AbortController();

    void streamSession({
      identity,
      sessionId,
      eventsPath: CODE_EVENTS_PATH,
      initialCursor: seen.current,
      signal: controller.signal,
      onFrame: (frame) => {
        // "rejected" is not "I have nothing to draw for this". It tells the
        // transport the frame failed local validation, which tears the
        // connection down and resumes from the last cursor -- and for a
        // quarantine notice the cursor has not moved past the notice yet, so
        // the same one arrives again and the stream never advances. What is
        // meant here is the opposite: the position was seen, move past it.
        //
        // Nothing is drawn for it. This pane answers "is it working or is it
        // stuck", and a position the server could not decode is not a step; the
        // record of what the turn did is the transcript, which is read back
        // from the server rather than assembled here.
        if (isQuarantineFrame(frame)) return "accepted";
        // Never reached: the transport routes a degraded notice to `onLiveGap`
        // and never offers it here. The branch exists so the envelope below is
        // known to be present, and returning "accepted" rather than "rejected"
        // keeps the unreachable case from being the one that wedges the stream.
        if (isDegradedFrame(frame)) return "accepted";
        // A live event has no position, so it cannot be resumed from; a
        // subscriber that mixed them would show a different list depending on
        // when it connected. The transport ignores what is returned for one of
        // these, so this says what is true rather than what has an effect.
        if (isLiveFrame(frame)) return "rejected";
        setEvents((current) => [...current, frame.envelope].slice(-KEPT_EVENTS));
        return "accepted";
      },
      onCursor: (next) => {
        seen.current = next;
      },
      onConnectionChange: () => undefined,
    });

    return () => {
      controller.abort();
      // Cleared on the way out rather than on the way in. Both empty the list
      // between turns, but clearing at the start does it one paint too late:
      // the section mounts with the previous turn's rows and drops them a frame
      // later, which reads as steps that ran and vanished.
      setEvents([]);
    };
  }, [identity, sessionId, watching]);

  return events;
}
