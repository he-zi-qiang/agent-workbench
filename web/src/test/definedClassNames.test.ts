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
 * 空的，而且是刚刚清空的。
 *
 * 装这条守卫时树上有十个悬空类名，按 `KNOWN_UNREAD_LEAVES` 的办法先冻在这里，
 * 好让守卫当天就能生效而不必先清完历史。十个里九个是**多余的钩子**——元素本来就
 * 被基类或父选择器管着（`.aw-icon-button`、`.aw-step-group > summary`、
 * `.aw-run-meta > :not(…)`），那个类名只是写下来没人接；删掉它们，渲染一个像素
 * 都没动。第十个 `.aw-code-output-note` 是真缺样式，补上了。
 *
 * 留着这张空表而不是删掉它：下一个人加进来时，需要有个地方写清楚"为什么这条可以
 * 先欠着"，而一张空表比一段注释更难被忽略。
 */
const KNOWN_UNSTYLED = new Set<string>([]);

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
