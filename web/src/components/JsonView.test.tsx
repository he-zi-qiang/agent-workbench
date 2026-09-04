import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JsonView, parseJsonDocument } from "./JsonView";

/**
 * 这个组件替掉的是 `<pre>{JSON.stringify(x, null, 2)}</pre>`，所以这里钉的是它
 * 和那个东西**不一样**的地方：换行是真换行、键是键值是值、太深的退回原样。
 */
describe("JsonView", () => {
  it("一个对象画成键 → 值，键在 dt 里，值在 dd 里", () => {
    render(<JsonView value={{ path: "docs/hello.html", recursive: true }} />);

    expect(screen.getByText("path").tagName).toBe("DT");
    expect(screen.getByText("docs/hello.html").tagName).toBe("SPAN");
    expect(screen.getByText("recursive").tagName).toBe("DT");
    expect(screen.getByText("true").tagName).toBe("CODE");
  });

  it("带换行的字符串是一块保留换行的正文，不是一行 \\n", () => {
    const content = "<!DOCTYPE html>\n<html>\n  <p>你好</p>\n</html>";
    const { container } = render(<JsonView value={{ content }} />);

    const block = container.querySelector("pre.aw-json-text");
    // 三个真换行，零个转义的 `\n`——这正是 JSON.stringify 做不到的那一半。
    expect(block?.textContent).toBe(content);
    expect(block?.textContent).not.toContain("\\n");
  });

  it("一列短标量并成一行，一列对象仍然一项一行", () => {
    const { container, rerender } = render(
      <JsonView value={["a.md", "b.md"]} />,
    );
    expect(container.querySelector(".aw-json-inline")).not.toBeNull();
    expect(container.querySelector("ol.aw-json-list")).toBeNull();

    rerender(<JsonView value={[{ name: "a" }, { name: "b" }]} />);
    expect(container.querySelectorAll("ol.aw-json-list > li")).toHaveLength(2);
  });

  it("嵌套往里缩一层，键仍然认得出是哪一层的", () => {
    const { container } = render(
      <JsonView value={{ error: { code: "tool_failed", retryable: false } }} />,
    );

    const outer = container.querySelector("dl.aw-json");
    const inner = outer?.querySelector("dd > dl.aw-json");
    expect(inner).not.toBeNull();
    expect(inner?.querySelector("dt")?.textContent).toBe("code");
  });

  it("空对象、空数组和 null 各有自己的写法，不画成一个空格子", () => {
    render(<JsonView value={{ a: {}, b: [], c: null }} />);

    expect(screen.getByText("{}")).toBeInTheDocument();
    expect(screen.getByText("[]")).toBeInTheDocument();
    expect(screen.getByText("null")).toBeInTheDocument();
  });

  it("六层以上退回 JSON 原文，那种东西不是给人看的", () => {
    let value: Record<string, unknown> = { leaf: 1 };
    for (let depth = 0; depth < 8; depth += 1) value = { down: value };
    const { container } = render(<JsonView value={value} />);

    expect(container.querySelector("pre.aw-json-text")?.textContent).toContain(
      '"leaf": 1',
    );
  });
});

describe("parseJsonDocument", () => {
  it("只认对象和数组", () => {
    expect(parseJsonDocument('{"a": 1}')).toEqual({ a: 1 });
    expect(parseJsonDocument(" [1, 2] ")).toEqual([1, 2]);
    // `"42"` 是合法 JSON，但它不是一份文档。
    expect(parseJsonDocument("42")).toBeUndefined();
    expect(parseJsonDocument('"text"')).toBeUndefined();
    expect(parseJsonDocument("null")).toBeUndefined();
  });

  it("以 { 开头但解析不了的散文按文本处理", () => {
    expect(parseJsonDocument("{not json")).toBeUndefined();
    // 被 4096 字节截断的参数摘要正是这种：半个 JSON。
    expect(parseJsonDocument('{"content": "<!DOCTYPE ht')).toBeUndefined();
  });
});
