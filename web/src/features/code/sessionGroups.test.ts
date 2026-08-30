import { describe, expect, it } from "vitest";
import type { CodeSessionView } from "../../api/types";
import { groupSessions, VISIBLE_SESSIONS } from "./sessionGroups";

function session(
  id: string,
  title: string | null,
  projectId: string | null = null,
): CodeSessionView {
  return {
    session_id: id,
    title,
    last_activity_at: null,
    project_id: projectId,
  };
}

const NAMES = new Map([
  ["p1", "agent 工作台"],
  ["p2", "cli-demo"],
]);

function grouped(
  sessions: CodeSessionView[],
  overrides: Partial<Parameters<typeof groupSessions>[1]> = {},
) {
  return groupSessions(sessions, {
    expanded: false,
    grouped: true,
    projectNames: NAMES,
    query: "",
    sessionId: undefined,
    ...overrides,
  });
}

describe("groupSessions", () => {
  it("groups by folder in the order the folders were last used", () => {
    // 列表本身按最后活动时间排，所以「先出现的组」就是「最近动过的文件夹」。
    // 按名字排会让今天一直在用的那个排到 M 后面去。
    const view = grouped([
      session("a", "第二个项目里的", "p2"),
      session("b", "第一个项目里的", "p1"),
      session("c", "第二个项目里的另一段", "p2"),
    ]);

    expect(view.groups.map((group) => group.label)).toEqual([
      "cli-demo",
      "agent 工作台",
    ]);
    expect(view.groups[0]?.sessions.map((one) => one.session_id)).toEqual(["a", "c"]);
  });

  it("names a folder it has no name for, and a session that has none", () => {
    // 少标一个是漏说，标错一个是撒谎：项目列表还没取回来时兜底成「别的文件夹」，
    // 而真的不属于任何文件夹是另一句话。
    const view = grouped([session("a", "一段", "p9"), session("b", "另一段", null)]);

    expect(view.groups.map((group) => group.label)).toEqual([
      "别的文件夹",
      "没有文件夹",
    ]);
  });

  it("does not name the group when the whole list is one folder", () => {
    // 收窄到一个文件夹时，那个名字不区分任何东西——而它已经写在列表上方
    // 「这个文件夹里的会话」那行字里了。
    const view = grouped([session("a", "一段", "p1")], { grouped: false });

    expect(view.groups).toHaveLength(1);
    expect(view.groups[0]?.label).toBeNull();
  });

  it("folds the tail away and says how many it folded", () => {
    const many = Array.from({ length: VISIBLE_SESSIONS + 5 }, (_, index) =>
      session(`s${String(index)}`, `第 ${String(index)} 段`, "p1"),
    );

    const view = grouped(many);

    expect(view.groups[0]?.sessions).toHaveLength(VISIBLE_SESSIONS);
    expect(view.hidden).toBe(5);
    expect(view.matched).toBe(VISIBLE_SESSIONS + 5);
    // 按过「全部显示」之后就不再折。
    expect(grouped(many, { expanded: true }).hidden).toBe(0);
  });

  it("keeps the open session visible even when it is past the fold", () => {
    // 它可能排在第 60 位——列表按 last_activity_at 排，而「打开」不算活动。一份
    // 「当前这一条不在里面」的列表，读者会以为自己丢了它。
    const many = Array.from({ length: VISIBLE_SESSIONS + 5 }, (_, index) =>
      session(`s${String(index)}`, `第 ${String(index)} 段`, "p1"),
    );

    const view = grouped(many, { sessionId: "s16" });
    const shown = view.groups.flatMap((group) => group.sessions);

    expect(shown.map((one) => one.session_id)).toContain("s16");
    // 挤掉最后一条，不是多画一条：这一栏的高度是定的。
    expect(shown).toHaveLength(VISIBLE_SESSIONS);
    expect(shown.map((one) => one.session_id)).not.toContain("s11");
  });

  it("searches names, ids and the folder a session is in", () => {
    const sessions = [
      session("aaa", "写一个解析器", "p1"),
      session("bbb", "改一个测试", "p2"),
      session("ccc", null, "p1"),
    ];

    expect(
      grouped(sessions, { query: "解析" }).groups.flatMap((g) => g.sessions),
    ).toHaveLength(1);
    // 文件夹名也能搜到：读者记得的常常是「那是在 cli-demo 里干的」。
    expect(
      grouped(sessions, { query: "cli-demo" }).groups.flatMap((g) => g.sessions),
    ).toHaveLength(1);
    // 没有名字的那一段（开了没用过）靠 id 找得到。
    expect(
      grouped(sessions, { query: "ccc" }).groups.flatMap((g) => g.sessions),
    ).toHaveLength(1);
  });

  it("does not fold while a search is narrowing the list", () => {
    // 读者已经自己收窄过一次了，再折起来一半是把同一件事做两遍。
    const many = Array.from({ length: VISIBLE_SESSIONS + 5 }, (_, index) =>
      session(`s${String(index)}`, `第 ${String(index)} 段`, "p1"),
    );

    const view = grouped(many, { query: "第" });

    expect(view.hidden).toBe(0);
    expect(view.groups[0]?.sessions).toHaveLength(VISIBLE_SESSIONS + 5);
  });
});
