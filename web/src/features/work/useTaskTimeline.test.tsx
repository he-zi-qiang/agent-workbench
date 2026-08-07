import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getTaskTimeline } from "../../api/client";
import type {
  EventEnvelope,
  PrincipalIdentity,
  TaskTimelineResponse,
} from "../../api/types";
import { useTaskTimeline } from "./useTaskTimeline";

vi.mock("../../api/client", () => ({
  getTaskTimeline: vi.fn(),
}));

const identity: PrincipalIdentity = {
  tenantId: "tenant_1",
  principalId: "owner_1",
  scopes: ["artifact:export"],
};

describe("useTaskTimeline", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(getTaskTimeline).mockReset();
  });

  it("polls incrementally, deduplicates replays, and resets at a task boundary", async () => {
    const first = envelope("event_1", "TaskSubmitted");
    const second = envelope("event_2", "TaskClaimed");
    const third = envelope("event_3", "TaskSubmitted", "task_2");
    vi.mocked(getTaskTimeline)
      .mockResolvedValueOnce(timeline("task_1", [first], "cursor_1"))
      .mockResolvedValueOnce(timeline("task_1", [first, second], "cursor_2"))
      .mockResolvedValueOnce(timeline("task_2", [third], "cursor_3"));

    const view = render(<Probe taskId="task_1" />);

    await waitFor(() => expect(screen.getByTestId("events")).toHaveTextContent("event_1"));
    expect(getTaskTimeline).toHaveBeenNthCalledWith(1, identity, "task_1", undefined);

    fireEvent.click(screen.getByRole("button", { name: "refresh timeline" }));
    await waitFor(() =>
      expect(screen.getByTestId("events")).toHaveTextContent("event_1,event_2"),
    );
    expect(getTaskTimeline).toHaveBeenNthCalledWith(2, identity, "task_1", "cursor_1");

    view.rerender(<Probe taskId="task_2" />);
    expect(screen.getByTestId("events")).toHaveTextContent("");
    await waitFor(() => expect(screen.getByTestId("events")).toHaveTextContent("event_3"));
    expect(getTaskTimeline).toHaveBeenNthCalledWith(3, identity, "task_2", undefined);
  });

  it("stops interval polling at a terminal snapshot but keeps manual refresh", async () => {
    vi.mocked(getTaskTimeline)
      .mockResolvedValueOnce(
        timeline(
          "task_terminal",
          [envelope("event_before", "TaskClaimed", "task_terminal")],
          "cursor_1",
        ),
      )
      .mockResolvedValueOnce(
        timeline(
          "task_terminal",
          [envelope("event_done", "TaskSucceeded", "task_terminal")],
          "cursor_done",
        ),
      )
      .mockResolvedValue(
        timeline("task_terminal", [], "cursor_done"),
      );

    render(
      <Probe intervalMs={10} pollingEnabled={false} taskId="task_terminal" />,
    );
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(3));
    expect(screen.getByTestId("events")).toHaveTextContent(
      "event_before,event_done",
    );
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(getTaskTimeline).toHaveBeenCalledTimes(3);

    fireEvent.click(screen.getByRole("button", { name: "refresh timeline" }));
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(4));
  });

  it("stops on a final event even while the caller still says the task is running", async () => {
    // The caller's `pollingEnabled` comes from a React Query that pauses
    // itself while `document.hidden`, so a backgrounded tab leaves it stuck at
    // "running" forever. This hook's own interval does not pause, so without
    // stopping on what it fetched it would poll a finished Task indefinitely.
    vi.mocked(getTaskTimeline)
      .mockResolvedValueOnce(
        timeline(
          "task_bg",
          [envelope("event_done", "TaskSucceeded", "task_bg")],
          "cursor_done",
        ),
      )
      .mockResolvedValue(timeline("task_bg", [], "cursor_done"));

    render(<Probe intervalMs={10} pollingEnabled taskId="task_bg" />);
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(1));

    await new Promise((resolve) => window.setTimeout(resolve, 80));

    expect(getTaskTimeline).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "refresh timeline" }));
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(2));
  });

  it("queues a forced refresh when the previous incremental request is active", async () => {
    let resolveFirst: ((response: TaskTimelineResponse) => void) | undefined;
    vi.mocked(getTaskTimeline)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValueOnce(
        timeline(
          "task_terminal",
          [envelope("event_done", "TaskSucceeded", "task_terminal")],
          "cursor_done",
        ),
      )
      .mockResolvedValueOnce(timeline("task_terminal", [], "cursor_done"));

    render(<Probe pollingEnabled={false} taskId="task_terminal" />);
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "refresh timeline" }));
    expect(getTaskTimeline).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst?.(timeline("task_terminal", [], "cursor_initial"));
      await Promise.resolve();
    });
    await waitFor(() => expect(getTaskTimeline).toHaveBeenCalledTimes(3));
    expect(getTaskTimeline).toHaveBeenNthCalledWith(
      2,
      identity,
      "task_terminal",
      "cursor_initial",
    );
  });
});

function Probe({
  intervalMs = 60_000,
  pollingEnabled = true,
  taskId,
}: {
  intervalMs?: number;
  pollingEnabled?: boolean;
  taskId: string;
}) {
  const timelineResult = useTaskTimeline(
    identity,
    taskId,
    intervalMs,
    pollingEnabled,
  );
  return (
    <>
      <output data-testid="events">
        {timelineResult.events.map((event) => event.event_id).join(",")}
      </output>
      <output data-testid="cursor">{timelineResult.cursor}</output>
      <button onClick={() => void timelineResult.refresh()} type="button">
        refresh timeline
      </button>
    </>
  );
}

function timeline(
  taskId: string,
  events: EventEnvelope[],
  cursor: string | null,
): TaskTimelineResponse {
  return { task_id: taskId, events, cursor };
}

function envelope(eventId: string, kind: string, taskId = "task_1"): EventEnvelope {
  return {
    schema_version: 1,
    event_id: eventId,
    stream_id: `stream_${taskId}`,
    run_id: `run_${taskId}`,
    event_type: kind,
    durability: "durable",
    timestamp: "2026-08-02T12:00:00Z",
    payload: { kind },
    sequence: null,
    task_id: taskId,
    graph_node_id: null,
    parent_event_id: null,
  };
}
