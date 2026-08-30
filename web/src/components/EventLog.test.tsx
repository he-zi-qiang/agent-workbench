/**
 * 事件流那一张。
 *
 * 这块面刻意什么都不解释，所以能出的错也就那么几种，而每一种都很安静：把上游那
 * 个数组原地倒过来、把一条失败事件画成灰色的普通行、给一条没有主语的事件编一个
 * 出来。三条各一个用例。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import { EventLog } from "./EventLog";

function event(overrides: Partial<EventEnvelope> = {}): EventEnvelope {
  return {
    schema_version: 1,
    event_id: "evt_1",
    stream_id: "str_1",
    run_id: "run_1",
    event_type: "RunStarted",
    durability: "durable",
    timestamp: "2026-08-30T09:25:11Z",
    payload: { kind: "test" },
    sequence: 1,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
    ...overrides,
  };
}

describe("顺序", () => {
  it("最新的在最上面", () => {
    render(
      <EventLog
        events={[
          event({ event_id: "a", event_type: "RunStarted" }),
          event({ event_id: "b", event_type: "RunCompleted" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]?.textContent).toContain("RunCompleted");
  });

  it("不原地改上游那个数组", () => {
    // `reverse()` 是原地的。直接对 props 调用它，会把页面正在拿来画对话的那份
    // 事件顺序也一起翻过来——一个只在打开过这张标签之后才出现的错。
    const events = [event({ event_id: "a" }), event({ event_id: "b" })];
    render(<EventLog events={events} />);
    expect(events.map((one) => one.event_id)).toEqual(["a", "b"]);
  });
});

describe("主语", () => {
  it("工具事件写工具名", () => {
    render(
      <EventLog
        events={[
          event({ event_type: "ToolStarted", payload: { kind: "tool", tool_name: "workspace_read" } }),
        ]}
      />,
    );
    expect(screen.getByText("workspace_read")).toBeInTheDocument();
  });

  it("取不到就不写，而不是编一个「未知」", () => {
    const { container } = render(<EventLog events={[event()]} />);
    expect(container.querySelector(".aw-event-log-subject")).toBeNull();
  });
});

describe("上色", () => {
  it("失败按后缀认，不靠一份会过期的名单", () => {
    // 名单式的写法过期时的样子是一条失败事件画成灰色的普通行——一个看起来正常
    // 的错。这里用一个名单里不可能有的类型来钉住后缀匹配。
    render(<EventLog events={[event({ event_type: "SomethingNewFailed" })]} />);
    expect(screen.getByRole("listitem")).toHaveClass("is-bad");
  });

  it("其余的是背景色", () => {
    render(<EventLog events={[event({ event_type: "RunStarted" })]} />);
    expect(screen.getByRole("listitem").className).toBe("");
  });
});

describe("空", () => {
  it("一条都没有时说出来，而不是画一个空框", () => {
    render(<EventLog events={[]} />);
    expect(screen.getByText("还没有事件。")).toBeInTheDocument();
  });
});
