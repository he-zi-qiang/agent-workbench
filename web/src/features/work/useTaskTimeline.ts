import { useCallback, useEffect, useRef, useState } from "react";
import { getTaskTimeline } from "../../api/client";
import type { EventEnvelope, PrincipalIdentity } from "../../api/types";
import {
  createTimelineState,
  mergeTimelineResponse,
  type TimelineState,
} from "./workTimeline";

/**
 * Events after which a Task produces no further ones.
 *
 * The timeline stops on these rather than only on the status query saying the
 * Task is terminal, because that query can stop answering. React Query pauses
 * `refetchInterval` while `document.hidden`; this hook's `setInterval` does
 * not pause. Background the tab mid-Task and the status freezes at "running"
 * while the timeline keeps polling a Task that finished minutes ago, for as
 * long as the tab stays in the background. Stopping on what the timeline has
 * itself fetched needs no second opinion.
 */
const FINAL_EVENTS = new Set([
  "TaskSucceeded",
  "TaskFailed",
  "TaskCancelled",
  "TaskDeadLettered",
]);

export interface TaskTimelineResult {
  events: EventEnvelope[];
  cursor: string | null;
  /**
   * Positions every page so far said it examined and could not deliver.
   *
   * Handed out beside `events` because the two are one answer: `events` alone
   * cannot say whether it is all of them, and a caller that shows the first
   * without the second is showing a partial history as a whole one.
   */
  skippedSequences: number[];
  /**
   * Whether this Task's own history says it is over.
   *
   * Exposed because the status query cannot always be the one to say so. It
   * pauses while `document.hidden` and this hook's `setInterval` does not, so
   * background a tab mid-Task and the timeline arrives at a finished Task
   * while the status stays at whatever it was when the tab went away. The
   * header then contradicts the timeline directly underneath it -- 排队中 above
   * a 运行已完成 -- until somebody reloads.
   *
   * Half of that gap was already closed here: the timeline stops polling on
   * what it fetched rather than waiting for a second opinion. This is the other
   * half, so the caller can go and get the status it stopped asking for.
   */
  settled: boolean;
  loading: boolean;
  error: unknown;
  refresh: () => Promise<void>;
}

interface TimelineHookState {
  requestKey: string;
  timeline: TimelineState;
  loaded: boolean;
  settled: boolean;
  error: unknown;
}

export function useTaskTimeline(
  identity: PrincipalIdentity,
  taskId: string | undefined,
  pollIntervalMs = 2_500,
  pollingEnabled = true,
): TaskTimelineResult {
  const identityKey = `${identity.tenantId}\u0000${identity.principalId}\u0000${identity.scopes.join(
    "\u0000",
  )}`;
  const requestKey = `${identityKey}\u0000${taskId ?? ""}`;
  const [state, setState] = useState<TimelineHookState>(() => ({
    requestKey,
    timeline: createTimelineState(taskId ?? ""),
    loaded: taskId === undefined,
    settled: false,
    error: null,
  }));
  const refreshRef = useRef<() => Promise<void>>(() => Promise.resolve());
  const pollingEnabledRef = useRef(pollingEnabled);

  useEffect(() => {
    pollingEnabledRef.current = pollingEnabled;
  }, [pollingEnabled]);

  useEffect(() => {
    let active = true;
    let inFlight: Promise<void> | null = null;
    let forcedAfterFlight: Promise<void> | null = null;
    let cursor: string | null = null;
    // Scoped to this effect, so selecting another Task starts over rather than
    // inheriting the last one's ending.
    let finished = false;
    const fixedTaskId = taskId;

    if (fixedTaskId === undefined) {
      refreshRef.current = () => Promise.resolve();
      return;
    }

    const performPoll = async (drain: boolean) => {
      try {
        let keepReading = true;
        while (active && keepReading) {
          const requestedCursor = cursor;
          const response = await getTaskTimeline(
            identity,
            fixedTaskId,
            cursor ?? undefined,
          );
          if (!active) return;
          if (response.task_id !== fixedTaskId) {
            throw new Error("任务时间线返回了不匹配的任务 ID");
          }
          cursor = response.cursor;
          if (response.events.some((event) => FINAL_EVENTS.has(event.event_type))) {
            finished = true;
          }
          const reachedEnd = finished;
          setState((previous) => {
            const carried = previous.requestKey === requestKey;
            const timeline = carried
              ? previous.timeline
              : createTimelineState(fixedTaskId);
            return {
              requestKey,
              timeline: mergeTimelineResponse(timeline, response),
              loaded: true,
              // Sticky within one Task, and only within one: `finished` is
              // scoped to this effect, so selecting another Task starts from
              // false rather than inheriting the last one's ending -- the same
              // rule the timeline itself follows.
              settled: reachedEnd || (carried && previous.settled),
              error: null,
            };
          });
          keepReading =
            drain &&
            response.events.length > 0 &&
            response.cursor !== requestedCursor;
        }
      } catch (caught) {
        if (active) {
          setState((previous) => {
            const carried = previous.requestKey === requestKey;
            return {
              requestKey,
              timeline: carried
                ? previous.timeline
                : createTimelineState(fixedTaskId),
              loaded: true,
              // A failed poll says nothing about whether the Task ended, so it
              // must not un-settle one already seen to have ended.
              settled: carried && previous.settled,
              error: caught,
            };
          });
        }
      }
    };

    const poll = (force = false): Promise<void> => {
      // `force` still gets through: the refresh button on a finished Task has
      // to be able to fetch, and so does the terminal-status effect.
      if (!active || (!force && (finished || !pollingEnabledRef.current))) {
        return Promise.resolve();
      }
      if (inFlight !== null) {
        if (!force) return inFlight;
        forcedAfterFlight ??= inFlight.then(() => {
          forcedAfterFlight = null;
          return poll(true);
        });
        return forcedAfterFlight;
      }

      const request = performPoll(!pollingEnabledRef.current);
      const trackedRequest = request.finally(() => {
        if (inFlight === trackedRequest) inFlight = null;
      });
      inFlight = trackedRequest;
      return trackedRequest;
    };

    refreshRef.current = () => poll(true);
    void poll(true);
    const timer = window.setInterval(() => void poll(false), pollIntervalMs);

    return () => {
      active = false;
      refreshRef.current = () => Promise.resolve();
      window.clearInterval(timer);
    };
  }, [identity, identityKey, pollIntervalMs, requestKey, taskId]);

  const refresh = useCallback(() => refreshRef.current(), []);
  const matchesRequest = state.requestKey === requestKey;
  return {
    events: matchesRequest ? state.timeline.events : [],
    cursor: matchesRequest ? state.timeline.cursor : null,
    // Gated on the same `matchesRequest` as the events they describe: a hole
    // belongs to one Task's history, and carrying it across a selection would
    // accuse the newly opened Task of damage that happened to another one.
    skippedSequences: matchesRequest ? state.timeline.skippedSequences : [],
    settled: matchesRequest && state.settled,
    loading: taskId !== undefined && (!matchesRequest || !state.loaded),
    error: matchesRequest ? state.error : null,
    refresh,
  };
}
