/**
 * 每一个写在 JSX 里的 `aw-*` 类名，样式表里都得真的有。
 *
 * **这条是被一个真 bug 逼出来的。** `ProviderKeyPanel` 引用了 `.aw-facts`，而那个
 * 类在三份样式表里一个都没有——于是那三行状态是没有任何样式的裸 `<dl>`：`dt` 和
 * `dd` 各占一行、缩进随浏览器默认。它不报错，不告警，`tsc` 过、`eslint` 过、
 * 组件测试也过，因为那些测试断言的是文字在不在，而文字确实在。**一个拼错的类名和
 * 一个还没写的类名，在这套工具链里长得一模一样。**
 *
 * 只看**写死**的类名，但「写死」包括模板串里那些静态的段。最初这条守卫把整个模板串
 * 都跳过了，理由是它们需要求值；代价是它对 `aw-app-shell`、`aw-chat-page`、
 * `aw-mobile-link` 这类**只出现在模板串里**的名字完全看不见——全仓 37 个。侧边栏那两个
 * 悬空类名（`aw-more-trigger`、`aw-sidebar-knowledge`）就是从这个洞里漏过去的：它们
 * 一个规则都没有，而守卫是绿的。
 *
 * 现在扫模板串，但只认**两侧都被空白或字面量边界夹住**的完整 token。`aw-${kind}-row`
 * 留下的静态段是 `aw-` 和 `-row`，两个都贴着插值，两个都不算数——所以扩大覆盖没有
 * 带进一条误报。求值才能得到的名字（`${on ? "aw-x" : ""}`）仍然看不见，那是这条守卫
 * 明确不管的部分。
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

/**
 * 模板串里挖掉 `${…}` 之后剩下的静态段，逐个 token 吐出来——但只吐**完整**的那些。
 *
 * 一个 token 贴着插值边界时，它可能只是某个求值结果的前缀或后缀（`aw-${k}-row` 里
 * 的 `aw-` 和 `-row`），那不是类名。所以段首的 token 只有在它同时是整个字面量的开头、
 * 或者它前面真的有空白时才算数；段尾同理。
 */
function* staticTokens(body: string): Generator<string> {
  const chunks: Array<{ text: string; atStart: boolean; atEnd: boolean }> = [];
  let cursor = 0;
  for (let index = 0; index < body.length; index += 1) {
    if (body[index] !== "$" || body[index + 1] !== "{") continue;
    chunks.push({ text: body.slice(cursor, index), atStart: cursor === 0, atEnd: false });
    // 数括号而不是找第一个 `}`：插值里可以再嵌对象或另一个模板串。
    let depth = 1;
    index += 2;
    for (; index < body.length && depth > 0; index += 1) {
      if (body[index] === "{") depth += 1;
      else if (body[index] === "}") depth -= 1;
    }
    cursor = index;
    index -= 1;
  }
  chunks.push({ text: body.slice(cursor), atStart: cursor === 0, atEnd: true });

  for (const chunk of chunks) {
    const tokens = chunk.text.split(/\s+/u);
    for (const [index, token] of tokens.entries()) {
      if (token === "") continue;
      if (index === 0 && !chunk.atStart) continue;
      if (index === tokens.length - 1 && !chunk.atEnd) continue;
      yield token;
    }
  }
}

function referencedClasses(): Map<string, Set<string>> {
  const used = new Map<string, Set<string>>();
  const note = (name: string, path: string) => {
    if (!name.startsWith("aw-")) return;
    const where = used.get(name) ?? new Set<string>();
    where.add(path);
    used.set(name, where);
  };

  for (const [path, source] of Object.entries(SOURCES)) {
    if (path.startsWith("test/")) continue;
    // `className="…"` 与 `className={"…"}`
    for (const [, literal] of source.matchAll(/className=\{?"([^"{}]*)"/gu)) {
      for (const name of (literal ?? "").split(/\s+/u)) note(name, path);
    }
    // className={`…`}。表达式里含反引号的会整条匹配不上——那是漏报，不是误报。
    for (const [, body] of source.matchAll(/className=\{`([^`]*)`\}/gu)) {
      for (const name of staticTokens(body ?? "")) note(name, path);
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
    // `aw-app-shell` 只出现在一处模板串里（AppShell.tsx 的根 div）。它在这里，
    // 就证明扫模板串那一半真的在跑——否则上面那条会退回只看字面量而依然是绿的。
    expect(used.has("aw-app-shell")).toBe(true);
  });

  it("模板串里贴着插值的半个名字不算类名", () => {
    // 这条守着扩大覆盖时唯一的真风险：`aw-${kind}-row` 的静态段是 `aw-` 和
    // `-row`，两个都不是类名。一旦这里开始误报，下一个人的修法会是往豁免表里
    // 加东西，那就等于把守卫关掉。
    expect([...staticTokens("aw-${kind}-row")]).toEqual([]);
    expect([...staticTokens("aw-card ${tone}")]).toEqual(["aw-card"]);
    expect([...staticTokens("${tone} aw-card")]).toEqual(["aw-card"]);
    expect([...staticTokens("aw-a ${x} aw-b")]).toEqual(["aw-a", "aw-b"]);
    expect([...staticTokens("aw-row ${on ? `aw-${x}` : \"\"} aw-tail")]).toEqual([
      "aw-row",
      "aw-tail",
    ]);
  });
});
