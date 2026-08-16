/**
 * One coding session's steps -- the whole session's, not just the running turn's.
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
 * **The list now outlives the turn.** It used to be emptied on the way out of
 * every turn, and gated on the turn running at all, which meant the process a
 * reader most wants to look at -- the one that just finished, whose result they
 * are reading -- was the one that had already been thrown away. Asking again
 * was impossible: steps are not in the transcript, and the transcript is all
 * that survived. So the subscription is now scoped to the *session*: it opens
 * when one is opened, replays that session's durable history from the start,
 * and is torn down only when the reader leaves for another session.
 *
 * What that costs: the transport's catch-up poll keeps running while a session
 * is open and idle, at `event_stream.catchup_poll_seconds`. That is the same
 * order as the approval poll this page already ran, and buying back the ability
 * to read a finished turn is worth one request a second against a loopback API.
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

/**
 * How many steps to keep. A turn is bounded, a session is not.
 *
 * Raised from 200 when the list stopped being per-turn: 200 covered a few
 * turns' worth of durable events and would now silently drop the oldest turn
 * of a long session. This is a memory bound, not a product decision -- a
 * session that outruns it loses its earliest steps, which is the least bad
 * thing to lose and the only one on offer without paging.
 */
const KEPT_EVENTS = 2000;

/** The events, and which session they belong to. */
interface Held {
  session: string;
  events: EventEnvelope[];
}

export function useCodeStream(
  identity: PrincipalIdentity,
  sessionId: string | undefined,
): EventEnvelope[] {
  // Carried with its session rather than emptied when the session changes,
  // which is the same shape `CodePage` uses for the transcript and the file
  // list, and for the same two reasons. Clearing from an effect is a render
  // late -- the previous session's steps are on screen for a frame, and for
  // the whole of the next session's first fetch -- and it is a `setState` in
  // an effect body, which the lint rule rejects for exactly that reason.
  const [held, setHeld] = useState<Held>({ session: "", events: [] });
  // The position belongs to the session too. It used to survive between turns
  // on purpose, so a new watch would not be re-served the previous turn's
  // steps; that was right while the list was per-turn and is wrong now that it
  // is per-session -- starting from no cursor is exactly how this replays a
  // session's whole history from sequence 1, which is the history the reader
  // came for.
  const seen = useRef<StreamCursor | null>(null);

  useEffect(() => {
    if (sessionId === undefined) return;
    const controller = new AbortController();
    // Not a `setState`, so it belongs here: the effect is keyed on the session,
    // which makes this the one place that runs exactly when the subject changes.
    seen.current = null;

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
        setHeld((current) =>
          // A frame that arrives for the session this list already holds
          // appends; one for a different session replaces. The second case is
          // how the list resets, and doing it here rather than in the effect
          // means it happens when the first row of the new session is ready to
          // draw -- never leaving an empty pane between the two.
          current.session === sessionId
            ? {
                session: sessionId,
                events: [...current.events, frame.envelope].slice(-KEPT_EVENTS),
              }
            : { session: sessionId, events: [frame.envelope] },
        );
        return "accepted";
      },
      onCursor: (next) => {
        seen.current = next;
      },
      onConnectionChange: () => undefined,
    });

    return () => {
      controller.abort();
      // No clearing here any more, and its absence is the fix. Emptying on the
      // way out is what destroyed a finished turn's steps: the turn ended,
      // `watching` went false, the effect tore down, and the process the reader
      // was about to look at went with it.
    };
  }, [identity, sessionId]);

  // Derived, so a session with nothing yet reads as empty rather than as the
  // previous session's steps.
  return held.session === sessionId ? held.events : [];
}
