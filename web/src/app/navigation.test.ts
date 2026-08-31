import { describe, expect, it } from "vitest";

import { NAVIGATION, QUICK_DESTINATIONS, isPathWithin } from "./navigation";

/**
 * 52 个测试文件此前没有一个 import 过 `QUICK_DESTINATIONS`，而快速跳转是
 * 键盘用户到达任何一页的主要方式。缺席的代价是具体的：关键词此前由一条
 * `item.to === ... ? ... : ...` 三元链给出，它漏掉了 `/usage`，于是用量页
 * 拿到的是 `/system` 的兜底串——搜「健康」「身份」命中用量页，搜「钱」
 * 「cost」一无所获。类型系统当时对此无话可说，因为兜底分支永远成立。
 */
describe("QUICK_DESTINATIONS", () => {
  it("covers every navigation entry exactly once", () => {
    expect(QUICK_DESTINATIONS.map((d) => d.to)).toEqual(
      NAVIGATION.map((item) => item.to),
    );
  });

  it("gives each destination its own keywords", () => {
    const keywords = QUICK_DESTINATIONS.map((d) => d.keywords);

    expect(new Set(keywords).size).toBe(keywords.length);
    for (const value of keywords) expect(value.trim()).not.toBe("");
  });

  it("routes the cost vocabulary to /usage and only there", () => {
    for (const term of ["钱", "cost", "token", "用量", "花费"]) {
      expect(matches(term)).toEqual(["/usage"]);
    }
  });

  it("routes the health vocabulary to /system and only there", () => {
    for (const term of ["健康", "身份", "status"]) {
      expect(matches(term)).toEqual(["/system"]);
    }
  });

  it("still finds the three workspaces by their Chinese aliases", () => {
    expect(matches("对话")).toEqual(["/chat"]);
    expect(matches("任务")).toEqual(["/work"]);
    expect(matches("编码")).toEqual(["/code"]);
  });

  it("puts the three workspaces in 工作 and the rest in 资源与工具", () => {
    const grouped = Object.fromEntries(
      QUICK_DESTINATIONS.map((d) => [d.to, d.group]),
    );

    expect(grouped["/chat"]).toBe("工作");
    expect(grouped["/work"]).toBe("工作");
    expect(grouped["/code"]).toBe("工作");
    expect(grouped["/usage"]).toBe("资源与工具");
  });
});

describe("isPathWithin", () => {
  it("matches a route root and its descendants", () => {
    expect(isPathWithin("/code", "/code")).toBe(true);
    expect(isPathWithin("/code/abc", "/code")).toBe(true);
  });

  it("refuses a lookalike prefix", () => {
    expect(isPathWithin("/codex", "/code")).toBe(false);
  });
});

/**
 * Mirrors `filterDestinations` in QuickSwitcher: label + group + description +
 * keywords, lower-cased, every term must appear. Duplicated rather than
 * exported so a change to the matcher shows up as a diff here too -- the point
 * of the assertions above is which page a person lands on, and that is a
 * property of both halves together.
 */
function matches(query: string): string[] {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return QUICK_DESTINATIONS.filter((destination) => {
    const haystack = [
      destination.label,
      destination.group,
      destination.description,
      destination.keywords,
    ]
      .join(" ")
      .toLocaleLowerCase();
    return terms.every((term) => haystack.includes(term));
  }).map((destination) => destination.to);
}
