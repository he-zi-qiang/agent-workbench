import { useMemo } from "react";

import type { EventEnvelope } from "../api/types";

/**
 * 事件流本身，倒序。
 *
 * **它是这一栏里唯一不解释的东西。** 旁边几张标签画的都是「这些事件合起来意味
 * 着什么」——第几步了、谁在跑、写出了什么。这一张画的是那些结论的**原料**：一
 * 条一条，按发生的顺序，不合并、不归纳、不挑选。
 *
 * 它存在的理由是前几张不够用的那些时刻：一个步骤显示成功而产物没出来、一个子代
 * 理的状态和它的用量对不上、一句「停在某某」而读者想知道在那之前发生了什么。那
 * 些时刻里，任何一层加工都在挡路。
 *
 * **倒序，因为最新的那条几乎总是要找的那条。** 一个还在跑的任务，读者要的是刚
 * 刚发生了什么；一个已经死掉的任务，要的是死之前那几条。两种都在这一头。
 *
 * **每条只写三样：时刻、类型、和那一条自己的主语。** 主语按类型取——工具事件取
 * 工具名，节点事件取节点名——取不到就不写，不编。载荷剩下的部分不画：它是给机器
 * 读的 JSON，铺在一条 420px 的栏里只会把「有哪些事件」这个问题也一起淹掉。要看
 * 全部载荷的人手上有 `GET /v1/tasks/{id}/timeline`，那才是它该待的地方。
 */

/** 一条事件的主语。取不到就是 `null`——不编一个「未知」出来。 */
function subjectOf(event: EventEnvelope): string | null {
  const payload = event.payload as {
    tool_name?: unknown;
    node?: unknown;
    node_id?: unknown;
    reason?: unknown;
  };
  for (const candidate of [
    payload.tool_name,
    payload.node,
    payload.node_id,
    event.graph_node_id,
  ]) {
    if (typeof candidate === "string" && candidate !== "") return candidate;
  }
  return null;
}

/** `2026-08-30T09:25:11Z` → `09:25:11`。日期不写：这一栏装的是一次运行。 */
function clockOf(timestamp: string): string {
  const at = new Date(timestamp);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString("zh-CN", { hour12: false });
}

/**
 * 出错和结束的那几类单独上色。
 *
 * 名单是**后缀**匹配而不是穷举：这个仓库的事件类型一直在加（`ToolFailed`、
 * `RunFailed`、`AgentFailed`……），穷举的名单过期时的样子是一条失败事件画成灰
 * 色的普通行——一个看起来正常的错。
 */
function toneOf(eventType: string): string {
  if (eventType.endsWith("Failed") || eventType.endsWith("Cancelled")) {
    return "is-bad";
  }
  if (eventType.endsWith("Completed")) return "is-good";
  return "";
}

export function EventLog({ events }: { events: readonly EventEnvelope[] }) {
  // 倒序在这里做一份拷贝：`events` 是上游持有的那个数组，`reverse()` 会原地改
  // 它，而画这一栏不该改任何人的数据。
  const rows = useMemo(() => [...events].reverse(), [events]);

  if (rows.length === 0) {
    return <p className="aw-event-log-empty">还没有事件。</p>;
  }

  return (
    <ol className="aw-event-log">
      {rows.map((event) => {
        const subject = subjectOf(event);
        return (
          <li className={toneOf(event.event_type)} key={event.event_id}>
            <time dateTime={event.timestamp}>{clockOf(event.timestamp)}</time>
            <span className="aw-event-log-type">{event.event_type}</span>
            {subject === null ? null : (
              <span className="aw-event-log-subject" title={subject}>
                {subject}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
