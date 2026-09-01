/**
 * 每一个写在 JSX 里的 `aw-*` 类名，样式表里都得真的有。
 *
 * **这条是被一个真 bug 逼出来的。** `ProviderKeyPanel` 引用了 `.aw-facts`，而那个
 * 类在三份样式表里一个都没有——于是那三行状态是没有任何样式的裸 `<dl>`：`dt` 和
 * `dd` 各占一行、缩进随浏览器默认。它不报错，不告警，`tsc` 过、`eslint` 过、
 * 组件测试也过，因为那些测试断言的是文字在不在，而文字确实在。**一个拼错的类名和
 * 一个还没写的类名，在这套工具链里长得一模一样。**
 *
 * 只看**字符串字面量**里的类名。模板串拼出来的、条件表达式算出来的都跳过——它们
 * 需要求值，而一个会误报的守卫最后会被人往豁免表里加东西，那就等于没有守卫。
 *
 * 走 vite 的 `import.meta.glob` 而不是 `node:fs`，理由和 `jsxChineseWrap` 那条一样：
 * 这个 tsconfig 里没有 node 的类型，给它加上等于让业务代码也能 `import fs`。
 */

import { describe, expect, it } from "vitest";

const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>("../**/*.{ts,tsx}", {
      query: "?raw",
      eager: true,
      import: "default",
    }),
  ).map(([key, text]) => [key.replace(/^\.\.\//u, ""), text]),
);

const STYLESHEETS: Record<string, string> = import.meta.glob<string>(
  "../styles/*.css",
  { query: "?raw", eager: true, import: "default" },
);

/**
 * 装这条守卫时就已经在树上的十个。**这张表只能变短。**
 *
 * 和 `tests/architecture/test_config_leaves_have_readers.py` 的 `KNOWN_UNREAD_LEAVES`
 * 同一个用法：一条新装的守卫如果要求先把历史清干净才能合，它就合不进来，而合不进
 * 来的守卫拦不住第十一个。所以把现状冻在这里，让守卫从今天起生效。
 *
 * 它们不是一类东西，混在一起记是因为工具链分不出来：
 *
 * - `aw-mode-start`、`aw-chat-ungrounded` 看起来是**标记类**——和一个确实有样式的
 *   类并排写，自己不带样式，给测试或脚本认。
 * - `aw-run-title`、`aw-run-tokens`、`aw-step-group-head` 这些看起来是**真没写**：
 *   `<span className="aw-run-title">` 单独出现，读起来是「这里该有个标题样式」，
 *   而实际上它什么也没做。
 *
 * 分清哪个是哪个要一条一条看，不在这次改动的范围里。下面第二条测试保证这张表里
 * 每一条**此刻仍然真的缺**——修好一个却忘了从表里删，会让这条红。
 */
const KNOWN_UNSTYLED = new Set<string>([
  "aw-chat-ungrounded",
  "aw-code-output-note",
  "aw-code-panel-collapse",
  "aw-mode-start",
  "aw-new-knowledge",
  "aw-preview-zoom",
  "aw-run-section-head",
  "aw-run-title",
  "aw-run-tokens",
  "aw-step-group-head",
]);

function definedClasses(): Set<string> {
  const defined = new Set<string>();
  for (const css of Object.values(STYLESHEETS)) {
    // 注释先去掉。这套样式表的注释里到处是类名——它们在解释某条规则为什么长这样，
    // 而一条**说明 `.aw-facts` 不存在**的注释，会让扫描以为 `.aw-facts` 存在。
    // 装这条守卫的当天就踩了一次：加完注释之后它对原来那个 bug 变成了绿的。
    const code = css.replace(/\/\*[\s\S]*?\*\//gu, "");
    for (const [, name] of code.matchAll(/\.(aw-[a-z0-9-]+)/gu)) {
      if (name !== undefined) defined.add(name);
    }
  }
  return defined;
}

function referencedClasses(): Map<string, Set<string>> {
  const used = new Map<string, Set<string>>();
  for (const [path, source] of Object.entries(SOURCES)) {
    if (path.startsWith("test/")) continue;
    // `className="…"` 与 `className={"…"}`，只认字面量。
    for (const [, literal] of source.matchAll(/className=\{?"([^"{}]*)"/gu)) {
      for (const name of (literal ?? "").split(/\s+/u)) {
        if (!name.startsWith("aw-")) continue;
        const where = used.get(name) ?? new Set<string>();
        where.add(path);
        used.set(name, where);
      }
    }
  }
  return used;
}

describe("样式表里没有的类名", () => {
  it("每一个 JSX 里写死的 aw-* 类名都有对应的 CSS", () => {
    const defined = definedClasses();
    const missing = [...referencedClasses()]
      .filter(([name]) => !defined.has(name) && !KNOWN_UNSTYLED.has(name))
      .map(([name, where]) => `${name} — ${[...where].sort().join(", ")}`)
      .sort();

    // 全部列出来而不是只报一个数：这条一旦红，读者要的是「哪几个、在哪个文件」。
    expect(missing).toEqual([]);
  });

  it("豁免表只能变短：里面每一条都必须此刻仍然真的缺", () => {
    const defined = definedClasses();
    const fixed = [...KNOWN_UNSTYLED].filter((name) => defined.has(name)).sort();
    expect(fixed).toEqual([]);
  });

  it("这个守卫自己看得见类名，而不是扫了个空", () => {
    // 没有这一条，一个坏掉的 glob 或正则会让上面那条永远绿。
    const used = referencedClasses();
    const defined = definedClasses();
    expect(used.size).toBeGreaterThan(100);
    expect(defined.size).toBeGreaterThan(100);
    expect(used.has("aw-button")).toBe(true);
    expect(defined.has("aw-button")).toBe(true);
  });
});
