import { describe, expect, it } from "vitest";
import { pageAmong, parentOf } from "./previewIntent";

describe("pageAmong", () => {
  it("picks the page, not the other things a turn wrote", () => {
    // 自动弹出只对能*跑起来*的那一种成立。源码和 Markdown 在对话里已经有卡片，
    // 再抢一次右栏是把同一件事说两遍。
    expect(pageAmong(["notes.md", "main.py", "index.html"])).toBe("index.html");
    expect(pageAmong(["notes.md", "main.py", "out.csv"])).toBeNull();
    expect(pageAmong([])).toBeNull();
  });

  it("takes the last page when a turn wrote several", () => {
    // 一轮里写 `parts.html` 再写 `index.html` 是常见顺序，收尾的那个才是成品。
    expect(pageAmong(["index.html", "draft.html"])).toBe("draft.html");
  });

  it("reads a declared type, and falls back to the name when it says nothing", () => {
    // 工作区条目带着服务端给的 media type，一个没有后缀的名字也因此认得出来。
    expect(pageAmong(["page"], () => "text/html")).toBe("page");
    // 项目文件那条路由一律答 `application/octet-stream`——那是「没人说过」，不是
    // 「说了它是字节」，所以名字仍然算数（`effectiveMediaType` 就是为这一种写
    // 的）。这一条不是细节：项目目录里写出的每一个页面都走它。
    expect(pageAmong(["page.html"], () => "application/octet-stream")).toBe(
      "page.html",
    );
    expect(pageAmong(["docs/report.htm"])).toBe("docs/report.htm");
    // 真的说了别的，就听它的：一个被存成 `text/plain` 的 `.html` 是一份源码。
    expect(pageAmong(["notes.html"], () => "text/plain")).toBeNull();
  });

  it("does not mistake a page for one of the kinds shown beside it", () => {
    // `previewKind` 把 SVG 留在 image 那一支（它在 `<img>` 里光栅化，脚本不跑），
    // 所以它不是「跑起来」的那一种，也就不该抢屏。
    expect(pageAmong(["diagram.svg"])).toBeNull();
    expect(pageAmong(["report.pdf"])).toBeNull();
  });
});

describe("parentOf", () => {
  it("answers the layer a directory listing would be asked for", () => {
    expect(parentOf("web/src/index.html")).toBe("web/src");
    // 根下的文件：`""` 就是「项目根」，正是 `listProjectFiles` 不带 path 的那一次。
    expect(parentOf("index.html")).toBe("");
  });
});
