import { useCallback, useEffect, useRef, useState } from "react";
import { getTaskTimeline } from "../../api/client";
import type { EventEnvelope, PrincipalIdentity } from "../../api/types";
import {
  createTimelineState,
  mergeTimelineResponse,
  type TimelineState,
} from "./workTimeline";

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
      if (!active || (!force && !pollingEnabledRef.current)) {
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
