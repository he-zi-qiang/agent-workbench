/**
 * 会话列表在被画出来之前，先被过滤、分组、截断。
 *
 * 三件事都是纯函数，因为它们各自都有一个说得清、也值得被钉住的规则，而在组件里它们
 * 会变成三段互相缠着的 JSX。
 *
 * **分组替掉的是一句重复。** 「全部会话」那一档此前在**每一行**上印它属于哪个文件
 * 夹——一份 40 条的列表里，同一个名字会出现 20 遍，而它要区分的东西只有 3 个。这是
 * 这一栏自己已经写过的那条规矩：同一件事在同一栏里被命名了两次，第二次占掉一整行
 * 高度。抬头说一次，底下那些行就只说自己的名字。
 *
 * **截断替掉的是无限长。** 这一栏是滚动的，所以长列表不会溢出——但一个滚动区里
 * 装着 80 条，读者要找的那一条在第 40 位时，这一栏就不再是「回到哪一段」的路，而是
 * 一次搜索任务。所以默认只画一屏，剩下的数出来。
 *
 * **正在看的那一段永远在里面。** 它可能排在第 60 位（很久以前开的，今天又打开了
 * 它——列表按 `last_activity_at` 排，而打开不算活动）。一份「当前这一条不在里面」的
 * 列表，读者会以为自己丢了它。
 */

import type { CodeSessionView } from "../../api/types";

/** 默认画几条。再多就折起来数出来。 */
export const VISIBLE_SESSIONS = 12;

export interface SessionGroup {
  key: string;
  /** `null` 表示这一组不需要抬头——收窄档只有一组，而抬头已经在上面那行字里了。 */
  label: string | null;
  sessions: CodeSessionView[];
}

export interface GroupedSessions {
  groups: SessionGroup[];
  /** 过滤之后一共有几条。截断前的那个数。 */
  matched: number;
  /** 被截断挡在外面的条数。0 表示没有折起来的东西。 */
  hidden: number;
}

/** 一行能被搜到的全部文字：名字、id，以及它属于哪个文件夹。 */
function haystack(
  session: CodeSessionView,
  projectNames: ReadonlyMap<string, string>,
): string {
  const folder =
    session.project_id == null ? "" : (projectNames.get(session.project_id) ?? "");
  return `${session.title ?? ""} ${session.session_id} ${folder}`.toLocaleLowerCase();
}

export function groupSessions(
  sessions: readonly CodeSessionView[],
  {
    expanded,
    grouped,
    projectNames,
    query,
    sessionId,
  }: {
    /** 读者按过「全部显示」。按过就不截断。 */
    expanded: boolean;
    /** 分不分组。收窄到一个文件夹时不分——那时候只有一组。 */
    grouped: boolean;
    projectNames: ReadonlyMap<string, string>;
    query: string;
    /** 屏幕上正开着的那一段，永远不会被截断挡掉。 */
    sessionId: string | undefined;
  },
): GroupedSessions {
  const needle = query.trim().toLocaleLowerCase();
  const matched =
    needle === ""
      ? [...sessions]
      : sessions.filter((session) => haystack(session, projectNames).includes(needle));

  // 搜索中不截断：读者已经自己收窄过一次了，再折起来一半是把同一件事做两遍。
  const truncating = !expanded && needle === "" && matched.length > VISIBLE_SESSIONS;
  const shown = truncating
    ? (() => {
        const head = matched.slice(0, VISIBLE_SESSIONS);
        if (sessionId === undefined) return head;
        if (head.some((session) => session.session_id === sessionId)) return head;
        const current = matched.find((session) => session.session_id === sessionId);
        // 挤掉最后一条，而不是多画一条：这一栏的高度是定的，而「当前这一段」比
        // 「第 12 近的那一段」更该被看见。
        return current === undefined ? head : [...head.slice(0, -1), current];
      })()
    : matched;

  if (!grouped) {
    return {
      groups: shown.length === 0 ? [] : [{ key: "all", label: null, sessions: shown }],
      matched: matched.length,
      hidden: matched.length - shown.length,
    };
  }

  // 按出现顺序建组，不按名字排：列表本身是按最后活动时间排的，所以「先出现的组」
  // 就是「最近动过的文件夹」。按名字排会让今天一直在用的那个文件夹排到 M 后面去。
  const order: string[] = [];
  const byKey = new Map<string, CodeSessionView[]>();
  for (const session of shown) {
    const key = session.project_id ?? "";
    const held = byKey.get(key);
    if (held === undefined) {
      order.push(key);
      byKey.set(key, [session]);
    } else {
      held.push(session);
    }
  }
  return {
    groups: order.map((key) => ({
      key: key === "" ? "none" : key,
      // 「别的文件夹」是项目列表还没取回来时的兜底，和这一栏此前在每一行上印的
      // 那个词一样——少标一个是漏说，标错一个是撒谎。
      label: key === "" ? "没有文件夹" : (projectNames.get(key) ?? "别的文件夹"),
      sessions: byKey.get(key) ?? [],
    })),
    matched: matched.length,
    hidden: matched.length - shown.length,
  };
}
