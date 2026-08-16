import { describe, expect, it } from "vitest";
import { summariseGroups, type StepGroup } from "./stepGroups";

function group(title: string, key: string): StepGroup {
  return { key, title, subject: null, outcome: "ok", events: [] };
}

function groups(...titles: string[]): StepGroup[] {
  return titles.map((title, index) => group(title, `k${String(index)}`));
}

describe("summariseGroups", () => {
  it("names what a stage did instead of counting it", () => {
    // The line this replaces read "16 步", which is the whole account of a
    // finished stage until somebody clicks it open.
    expect(
      summariseGroups(
        groups(
          ...Array<string>(12).fill("读取网页"),
          ...Array<string>(3).fill("搜索网络"),
          "模型作答",
        ),
      ),
    ).toBe("读取网页 ×12 · 搜索网络 ×3 · 模型作答");
  });

  it("drops the ×1 that says nothing the title does not", () => {
    expect(summariseGroups(groups("写入工作区"))).toBe("写入工作区");
  });

  it("stops at three kinds and says how many it stopped at", () => {
    // Past three this is no longer a line; it is the list it stands in for.
    expect(
      summariseGroups(groups("甲", "甲", "乙", "丙", "丁", "戊")),
    ).toBe("甲 ×2 · 乙 · 丙 · 等 2 项");
  });

  it("orders by count, and ties by which appeared first", () => {
    // Stable across polls: a digest that reshuffled when two kinds drew level
    // would look like the stage had changed when only the sort had.
    expect(summariseGroups(groups("乙", "甲", "甲", "乙"))).toBe("乙 ×2 · 甲 ×2");
  });

  it("says nothing about a stage that has done nothing", () => {
    expect(summariseGroups([])).toBe("");
  });
});
