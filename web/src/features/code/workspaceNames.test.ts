import { describe, expect, it } from "vitest";
import {
  describeFolderUpload,
  flattenPath,
  planFolderUpload,
} from "./workspaceNames";

/** 一个带 `webkitRelativePath` 的 File，浏览器目录选择器给的就是这个形状。 */
function entry(path: string): File {
  const file = new File(["x"], path.split("/").at(-1) ?? path);
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

function list(...paths: string[]): FileList {
  const files = paths.map(entry);
  return {
    ...files,
    length: files.length,
    item: (index: number) => files[index] ?? null,
    [Symbol.iterator]: function* () {
      yield* files;
    },
  } as unknown as FileList;
}

describe("flattenPath", () => {
  it("turns separators into a mark instead of deleting them", () => {
    // 删掉会把 `a/b.ts` 和 `ab.ts` 折成同一个名字，而折叠之后的碰撞事后解释不清
    // ——`adapters/mcp/naming.py` 拒绝静默删除字符时给的是同一条理由。
    expect(flattenPath("src/app/main.ts")).toBe("src-app-main.ts");
    expect(flattenPath("src/app/main.ts")).not.toBe(flattenPath("srcappmain.ts"));
  });

  it("drops the leading characters `WorkspaceName` forbids", () => {
    // 首字符必须是字母数字，所以 `.` 和 `..` 连名字都不是。
    expect(flattenPath("cfg/.env")).toBe("cfg-.env");
    expect(flattenPath(".env")).toBe("env");
    expect(flattenPath("--weird")).toBe("weird");
  });

  it("never returns something the server would refuse", () => {
    const pattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/;
    for (const path of [
      "文档/说明.md",
      "a b/c d.txt",
      "x".repeat(400),
      "深/很深/更深/notes.md",
      "!!!",
    ]) {
      expect(flattenPath(path)).toMatch(pattern);
    }
  });

  it("keeps the extension when it has to cut", () => {
    // 从右边一刀切会让预览和 media_type 一起失准，而扩展名是这个名字在界面上
    // 唯一还被读的部分。
    const cut = flattenPath(`${"a".repeat(400)}.md`);

    expect(cut.endsWith(".md")).toBe(true);
    expect(cut.length).toBeLessThanOrEqual(128);
  });

  it("falls back to a name rather than to an empty one", () => {
    // 中文文件名压完可能一个合法字符都不剩，而空名字会被服务端 422——那时读者
    // 已经等了二十个上传。
    expect(flattenPath("中文/名字")).toBe("file");
  });
});

describe("planFolderUpload", () => {
  it("skips the directories nobody meant to upload, and counts them", () => {
    // 浏览器给的是整棵树，没有 .gitignore 这一说。一个真实仓库里 node_modules
    // 常常是文件数的九成，而工作区一共只收 256 条。
    const plan = planFolderUpload(
      list("p/src/a.ts", "p/node_modules/x/index.js", "p/.git/HEAD"),
    );

    expect(plan.entries.map((one) => one.name)).toEqual(["p-src-a.ts"]);
    expect(plan.skipped).toBe(2);
  });

  it("keeps a dotfile that is a file, not a directory", () => {
    const plan = planFolderUpload(list("p/.gitignore"));

    expect(plan.skipped).toBe(0);
    expect(plan.entries[0]?.name).toBe("p-.gitignore");
  });

  it("suffixes a collision instead of letting one overwrite the other", () => {
    // 工作区的写是 compare-and-set，覆盖不报错——只会安静地少一个文件。
    const plan = planFolderUpload(list("p/a/x.ts", "p/a-x.ts"));

    expect(plan.entries.map((one) => one.name)).toEqual(["p-a-x.ts", "p-a-x-2.ts"]);
    expect(plan.renamed).toBe(1);
  });

  it("puts the suffix before the extension", () => {
    const plan = planFolderUpload(list("p/a/x.ts", "p/a-x.ts", "p/a/x.ts"));

    expect(plan.entries[2]?.name).toBe("p-a-x-3.ts");
  });
});

describe("describeFolderUpload", () => {
  it("shows a real example of the renaming, not an abstract note", () => {
    // 读者对「名字会变」的想象和压出来的结果往往不是一回事，而他事后在工作区里
    // 找不到 main.ts 时会以为上传失败了。
    const said = describeFolderUpload(
      planFolderUpload(list("p/src/main.ts", "p/node_modules/x.js")),
    );

    expect(said).toContain("1 个文件");
    expect(said).toContain("p/src/main.ts → p-src-main.ts");
    expect(said).toContain("node_modules");
  });
});
