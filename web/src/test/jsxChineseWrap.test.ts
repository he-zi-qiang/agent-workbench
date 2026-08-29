/**
 * JSX 里的中文段落不许折行，因为折行会在两个汉字之间留下一个看得见的空格。
 *
 * JSX 把源码里的换行加缩进折成**一个空格**。对英文这是对的——词与词之间本来就有
 * 空格；对中文是错的，字与字之间没有。所以
 *
 * ```tsx
 * <p>
 *   其它工具的作用域是这个进程自己的工作区、数据库、沙箱容器。这一个的
 *   作用域是运行 Worker 的那台机器本身。
 * </p>
 * ```
 *
 * 在浏览器里渲染成「……这一个的 作用域是……」，DOM 的 `textContent` 里真的多了一个
 * U+0020，不是折行造成的视觉效果。2026-08-29 把构建产物挂在 `agent-api --web-dir`
 * 上实测，`/ui/#/computer` 一页就有 12 段命中；全仓 54 处、11 个文件，同批清完。
 *
 * **为什么是一条测试而不是一条约定。** 这个仓库的中文散文是逐句写的，写的时候手会
 * 自然地在 80 列附近折行——这条缺陷不需要谁犯错就会长回来，它是编辑这些段落的默认
 * 后果。约定拦不住默认行为，门禁可以。
 *
 * **为什么用 TypeScript 的 parser 而不是正则。** 第一版用正则扫「行尾是汉字、下一行
 * 行首也是汉字」，报出 186 处——虚报了 3.4 倍。三类假阳性：多行中文注释（渲染不到）、
 * 模板字符串（换行是真换行）、以及 `</strong>` 换行接中文那一类——那个换行落在
 * JsxText 节点的**开头**，JSX 会把它整段删掉，一个空格都不注入。正则分不出「节点内部
 * 的换行」和「节点开头的换行」，AST 分得出，而这两者一个是缺陷、一个完全正常。
 *
 * 修法是把两行并成一行。**不是** `{""}`：那能达到同样的效果，但会在每一段面向读者的
 * 中文里插一个需要解释的记号，而这一条本来就没有需要读者知道的部分。代价是几行很长的
 * 散文行（最长 265 字符，`ComputerPage` 那段讲多屏坐标的）——`eslint` 没有行宽规则，
 * 而一段散文占一行在 diff 里比七行各加五个字符更好读。
 */

import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * 每个 .tsx 的源码，按 `src/` 下的相对路径。
 *
 * 走 vite 的 `import.meta.glob` 而不是 `node:fs`，理由不是风格：这个 tsconfig 里
 * 没有 node 的类型，而给它加上等于让业务代码也能 `import fs`——一条门禁测试不该
 * 换来那个。`?raw` 拿到的就是磁盘上的字节，和读文件是同一件事。
 */
const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>("../**/*.tsx", {
      query: "?raw",
      eager: true,
      import: "default",
    }),
  ).map(([key, text]) => [key.replace(/^\.\.\//u, ""), text]),
);

/** 汉字，以及会出现在行尾或行首的全角标点。 */
const CJK = "\\u4e00-\\u9fff\\u3400-\\u4dbf";
const TAIL = "。，、；：！？）」』》…—";
const HEAD = "（「『《";
const WRAPPED = new RegExp(
  `([${CJK}${TAIL}])[ \\t]*\\n[ \\t]*([${CJK}${HEAD}])`,
  "u",
);

/**
 * 每一处「JSX 会在这里注入一个空格」的位置，带上下文。
 *
 * 收 source 文本而不是路径，这样检测逻辑本身可以被喂合成源码——见下面第二、三条
 * 测试。只按文件跑的话，检测器漏报时没有任何东西会红。
 */
function wrappedChineseIn(label: string, text: string): string[] {
  const source = ts.createSourceFile(
    label,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const found: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) {
      const raw = node.getFullText(source);
      let cursor = 0;
      for (;;) {
        const match = WRAPPED.exec(raw.slice(cursor));
        if (match === null) break;
        const { line } = source.getLineAndCharacterOfPosition(
          node.getFullStart() + cursor + match.index,
        );
        found.push(
          `${label}:${String(line + 1)} …${match[0].replace(/\s+/gu, "␣")}…`,
        );
        cursor += match.index + match[0].length;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

describe("JSX 里的中文段落不折行", () => {
  it("全仓没有任何一段中文因为折行而多出一个空格", () => {
    const offenders = Object.entries(SOURCES).flatMap(([file, text]) =>
      wrappedChineseIn(file, text),
    );

    // 扫到了东西这件事本身也要成立：一个 glob 写错的实现会交出空表，然后这条断言
    // 永远绿。
    expect(Object.keys(SOURCES).length).toBeGreaterThan(20);

    // 全部列出来而不是只报一个数：这条一旦红，读者要的是「去哪一行把两行并起来」，
    // 而不是「有几处」。
    expect(offenders).toEqual([]);
  });

  it("认得出一段被折了行的中文", () => {
    // 检测器自己的正样本。没有它，一个什么都不报的实现能让上面那条永远绿。
    const found = wrappedChineseIn(
      "sample.tsx",
      ["const A = () => (", "  <p>", "    第一行结尾", "    第二行开头", "  </p>", ");"].join(
        "\n",
      ),
    );

    expect(found).toHaveLength(1);
    expect(found[0]).toContain("尾␣第");
  });

  it("`</strong>` 之后换行接中文不算——那个换行 JSX 本来就会删掉", () => {
    // 负样本，而且是把第一版正则实现打红的那一个。JSX 删掉文本节点**开头**的
    // 换行空白，所以这里一个空格都不注入；一个按行扫的实现会把它报成缺陷，然后
    // 有人去「修」一个不存在的空格，把源码改坏。
    const found = wrappedChineseIn(
      "sample.tsx",
      [
        "const A = () => (",
        "  <p>",
        "    <strong>标题</strong>",
        "    说明文字",
        "  </p>",
        ");",
      ].join("\n"),
    );

    expect(found).toEqual([]);
  });

  it("中文注释与模板字符串里的同样形状不算", () => {
    // 另外两类假阳性。它们渲染不到页面上，而模板字符串里的换行是真换行——把它并
    // 起来会改变输出。
    const found = wrappedChineseIn(
      "sample.tsx",
      [
        "// 这是一行注释的结尾",
        "// 下一行注释的开头",
        "const text = `模板字符串结尾",
        "模板字符串开头`;",
        "const A = () => <p>{text}</p>;",
      ].join("\n"),
    );

    expect(found).toEqual([]);
  });
});
