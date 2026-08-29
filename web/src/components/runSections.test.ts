import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import { foldForeignRuns, hasForeignRun, splitByRun } from "./runSections";

const PARENT = "run_parent";
const CHILD = "run_child";
const OTHER = "run_other";

function event(runId: string, sequence: number): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `evt_${String(sequence)}`,
    stream_id: "thr_1",
    run_id: runId,
    event_type: "ModelStarted",
    durability: "durable",
    timestamp: "2026-08-29T04:00:00Z",
    payload: { kind: "ModelStarted" },
    sequence,
    task_id: "task_1",
    graph_node_id: "work",
    parent_event_id: null,
  };
}

describe("按运行切段", () => {
  it("没有事件就没有段", () => {
    expect(splitByRun([])).toEqual([]);
  });

  it("只有一个运行时是一整段，而且是「自己的」", () => {
    // 这条决定了绝大多数任务看不出任何变化：从没委派过的阶段切出来只有一段，
    // 调用方照旧内联渲染。
    const sections = splitByRun([event(PARENT, 1), event(PARENT, 2)]);

    expect(sections).toHaveLength(1);
    expect(sections[0]?.own).toBe(true);
    expect(sections[0]?.events).toHaveLength(2);
    expect(hasForeignRun(sections)).toBe(false);
  });

  it("父 → 子 → 父切成三段，顺序不动", () => {
    // 归堆会把父运行在委派**之后**做的事挪到子运行前面。这条流存在的理由就是顺序。
    const sections = splitByRun([
      event(PARENT, 1),
      event(PARENT, 2),
      event(CHILD, 3),
      event(CHILD, 4),
      event(PARENT, 5),
    ]);

    expect(sections.map((s) => [s.runId, s.own, s.events.length])).toEqual([
      [PARENT, true, 2],
      [CHILD, false, 2],
      [PARENT, true, 1],
    ]);
    expect(hasForeignRun(sections)).toBe(true);
  });

  it("同一个子运行被穿插两次就是两段", () => {
    // 不是缺陷，是实际发生的事：两次委派之间父运行做了别的。
    const sections = splitByRun([
      event(PARENT, 1),
      event(CHILD, 2),
      event(PARENT, 3),
      event(CHILD, 4),
    ]);

    expect(sections.map((s) => s.runId)).toEqual([PARENT, CHILD, PARENT, CHILD]);
  });

  it("一条事件都不丢，顺序也不变", () => {
    // 和 `stepGroups` 同一条承诺。一个认不出 run_id 的实现最容易的错法就是把它
    // 悄悄扔掉，而丢掉的那条在界面上和「没发生过」长得一样。
    const events = [
      event(PARENT, 1),
      event(CHILD, 2),
      event(OTHER, 3),
      event(CHILD, 4),
      event(PARENT, 5),
    ];

    const flat = splitByRun(events).flatMap((section) => section.events);
    expect(flat).toEqual(events);
  });

  it("并排的两个子代理各自成段", () => {
    const sections = splitByRun([
      event(PARENT, 1),
      event(CHILD, 2),
      event(OTHER, 3),
    ]);

    expect(sections.map((s) => [s.runId, s.own])).toEqual([
      [PARENT, true],
      [CHILD, false],
      [OTHER, false],
    ]);
  });

  it("第一条事件是谁的，谁就是「自己的」——哪怕它写得最少", () => {
    // 判据是位置不是数量。一次委派只可能发生在父运行已经开始之后，所以最早的那条
    // 必然是父运行的；而一个搜了二十次的子代理，事件数可以轻松盖过派它出去的那个。
    const sections = splitByRun([
      event(PARENT, 1),
      ...Array.from({ length: 20 }, (_unused, index) => event(CHILD, index + 2)),
    ]);

    expect(sections[0]?.own).toBe(true);
    expect(sections[0]?.runId).toBe(PARENT);
    expect(sections[1]?.own).toBe(false);
    expect(sections[1]?.events).toHaveLength(20);
  });

  it("缺页导致最早一条是子运行时，主次会颠倒——而它仍然只说真话", () => {
    // 这条钉的是一个**已知的降级**，不是期望的行为。文档写在 `runSections.ts` 的
    // `own` 字段上：这时候被装进框里的是父运行，但「这些事件来自另一个运行」这句
    // 话仍然成立，而缺页本身在时间线上方另有提示。
    const sections = splitByRun([event(CHILD, 3), event(PARENT, 5)]);

    expect(sections[0]?.own).toBe(true);
    expect(sections[0]?.runId).toBe(CHILD);
    expect(sections[1]?.own).toBe(false);
  });
});

describe("把同一个别人的运行并成一块", () => {
  it("并发交错的两个子代理各自只剩一块", () => {
    // 真数据的形状：`task_75cd1e0c` 四个 analyst、并发上限 2，事件层面是
    // c1 → c2 → c1 → c2 交替。只切不并会摊成十个一两条的小块。
    const folded = foldForeignRuns(
      splitByRun([
        event(PARENT, 1),
        event(CHILD, 2),
        event(OTHER, 3),
        event(CHILD, 4),
        event(OTHER, 5),
      ]),
    );

    expect(folded.map((s) => [s.runId, s.events.length])).toEqual([
      [PARENT, 1],
      [CHILD, 2],
      [OTHER, 2],
    ]);
  });

  it("父运行的段一条都不并，位置也不动", () => {
    // 它的事件之间有真正的先后：它委派、它等、它拿到结果、它接着做。而两个并发
    // 子代理之间没有。
    const folded = foldForeignRuns(
      splitByRun([
        event(PARENT, 1),
        event(CHILD, 2),
        event(PARENT, 3),
        event(CHILD, 4),
        event(PARENT, 5),
      ]),
    );

    expect(folded.map((s) => [s.runId, s.events.length])).toEqual([
      [PARENT, 1],
      [CHILD, 2],
      [PARENT, 1],
      [PARENT, 1],
    ]);
  });

  it("一条事件都不丢", () => {
    const events = [
      event(PARENT, 1),
      event(CHILD, 2),
      event(OTHER, 3),
      event(CHILD, 4),
      event(PARENT, 5),
    ];
    const folded = foldForeignRuns(splitByRun(events));

    expect(folded.flatMap((s) => s.events)).toHaveLength(events.length);
    expect(new Set(folded.flatMap((s) => s.events.map((e) => e.event_id))).size).toBe(
      events.length,
    );
  });

  it("被并起来的块里，事件保持它们自己的先后", () => {
    // 合并跨过的是**别的**运行，不是这个运行自己的顺序。
    const folded = foldForeignRuns(
      splitByRun([
        event(PARENT, 1),
        event(CHILD, 2),
        event(OTHER, 3),
        event(CHILD, 4),
        event(OTHER, 5),
        event(CHILD, 6),
      ]),
    );

    const child = folded.find((s) => s.runId === CHILD);
    expect(child?.events.map((e) => e.sequence)).toEqual([2, 4, 6]);
  });

  it("`own` 的判据不被合并改掉", () => {
    // 缺页导致最早一条是子运行时，那个子运行是 `own`，它的段一个都不并——
    // 这是降级场景下的行为，钉住它免得 fold 顺手把 own 也归了堆。
    const folded = foldForeignRuns(
      splitByRun([event(CHILD, 1), event(PARENT, 2), event(CHILD, 3)]),
    );

    expect(folded.map((s) => [s.runId, s.own, s.events.length])).toEqual([
      [CHILD, true, 1],
      [PARENT, false, 1],
      [CHILD, true, 1],
    ]);
  });

  it("没有别人的运行时原样返回", () => {
    const sections = splitByRun([event(PARENT, 1), event(PARENT, 2)]);
    expect(foldForeignRuns(sections)).toEqual(sections);
  });
});
