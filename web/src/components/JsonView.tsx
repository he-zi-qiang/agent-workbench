/**
 * 一段 JSON，画成人读的样子。
 *
 * **它替掉的是 `<pre>{JSON.stringify(x, null, 2)}</pre>`。** 那个东西对机器是忠实的，
 * 对人是错的：`workspace_write` 的参数是 `{"content": "<!DOCTYPE html>\n<html>…",
 * "name": "x.html"}`，而一整个文件在 JSON 字符串里是**一行**，换行是 `\n` 两个字
 * 符、引号是 `\"` 三个字符。读者打开一步想看「它写了什么」，看到的是一行三千字
 * 的转义。用户的原话是「里面的 json 文件，展现的不是很好」。
 *
 * 所以这里按**值的形状**画，不按文本画：一个对象是「键 → 值」的两列，一个数组是
 * 一列，一段带换行的字符串是一块保留换行的正文，一个数或布尔是一个短码。嵌套就
 * 往里缩一层。键还是键，值还是值，只是不再要读者自己在脑子里做 `JSON.parse`。
 *
 * **不是编辑器，也不折叠。** 折叠是给「我不知道里面有什么」的人的，而一步的参数
 * 通常就三五个键；一个要点开的三角比多看两行更贵。太深的嵌套（六层以上）退回
 * `JSON.stringify`：那种东西不是给人看的，画出来也没人读。
 *
 * **只认对象和数组。** `"42"` 是合法 JSON，但它不是一份文档；一段碰巧以 `{` 开头
 * 的散文也不是——`parseJsonDocument` 只在整段解析成功且是对象或数组时才给值，
 * 否则调用方照旧当文本画。
 */

import type { ReactNode } from "react";

/** 超过这个深度就不再一层层画了。 */
const MAX_DEPTH = 6;

/** 字符串多长、或带换行，就该是一块正文而不是一行。 */
const INLINE_STRING_MAX = 100;

/**
 * 整段解析成对象或数组，否则 `undefined`。
 *
 * `undefined` 而不是 `null`：`null` 本身就是一个合法的 JSON 值，用它当「不是
 * 文档」会把 `"null"` 这段文本和「解析失败」混成一个答案。
 */
export function parseJsonDocument(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return undefined;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return typeof parsed === "object" && parsed !== null ? parsed : undefined;
  } catch {
    return undefined;
  }
}

export function JsonView({ value, depth = 0 }: { value: unknown; depth?: number }) {
  return <>{renderValue(value, depth)}</>;
}

function renderValue(value: unknown, depth: number): ReactNode {
  if (value === null) return <code className="aw-json-null">null</code>;
  if (typeof value === "string") return renderString(value);
  if (typeof value === "number" || typeof value === "boolean") {
    return <code className="aw-json-scalar">{String(value)}</code>;
  }
  if (depth >= MAX_DEPTH) {
    return <pre className="aw-json-text">{JSON.stringify(value, null, 2)}</pre>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <code className="aw-json-null">[]</code>;
    // 一列短标量并成一行：`["a.md", "b.md"]` 竖着画三行是在浪费读者的眼睛。
    if (value.length <= 8 && value.every(isShortScalar)) {
      return (
        <span className="aw-json-inline">
          {value.map((item, index) => (
            // 位置就是身份：这是一个只读的固定数组，同一个值可以出现两次。
            <span key={index}>{renderValue(item, depth + 1)}</span>
          ))}
        </span>
      );
    }
    return (
      <ol className="aw-json-list">
        {value.map((item, index) => (
          <li key={index}>{renderValue(item, depth + 1)}</li>
        ))}
      </ol>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <code className="aw-json-null">{"{}"}</code>;
    return (
      <dl className="aw-json">
        {entries.map(([key, held]) => (
          <div className="aw-json-row" key={key}>
            <dt>{key}</dt>
            <dd>{renderValue(held, depth + 1)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  // 剩下的只有 undefined、函数、symbol、bigint——JSON 里没有它们，能到这里只
  // 能是调用方递了一个不是从 JSON 来的值。写出类型名，不写 `[object Object]`。
  return (
    <code className="aw-json-null">
      {typeof value === "bigint" ? `${value.toString()}n` : typeof value}
    </code>
  );
}

function renderString(text: string): ReactNode {
  if (text === "") return <code className="aw-json-null">“”</code>;
  if (text.includes("\n") || text.length > INLINE_STRING_MAX) {
    return <pre className="aw-json-text">{text}</pre>;
  }
  return <span className="aw-json-string">{text}</span>;
}

function isShortScalar(value: unknown): boolean {
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    return true;
  }
  return (
    typeof value === "string" && !value.includes("\n") && value.length <= 40
  );
}
