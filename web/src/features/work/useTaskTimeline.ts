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
  loading: boolean;
  error: unknown;
  refresh: () => Promise<void>;
}

interface TimelineHookState {
  requestKey: string;
  timeline: TimelineState;
  loaded: boolean;
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
          setState((previous) => {
            const timeline =
              previous.requestKey === requestKey
                ? previous.timeline
                : createTimelineState(fixedTaskId);
            return {
              requestKey,
              timeline: mergeTimelineResponse(timeline, response),
              loaded: true,
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
          setState((previous) => ({
            requestKey,
            timeline:
              previous.requestKey === requestKey
                ? previous.timeline
                : createTimelineState(fixedTaskId),
            loaded: true,
            error: caught,
          }));
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
    loading: taskId !== undefined && (!matchesRequest || !state.loaded),
    error: matchesRequest ? state.error : null,
    refresh,
  };
}
