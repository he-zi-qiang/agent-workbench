/**
 * 模型此刻在浏览器里看到的那一帧（ADR-0113 §3.6）。
 *
 * **它是只读的，而且这一点是故意做不出别的样子的。** 面板里没有地址栏、没有
 * 可点的区域、没有任何能把输入送回去的东西——ADR-0113 §4 拒绝让人直接操作这个
 * 浏览器，理由是两个操作者同时驱动一个页面需要一套仲裁规则，而那套规则还不
 * 存在。与其写一个"暂时禁用"的按钮，不如让这块面根本没有那条路。
 *
 * **三种状态，不能合并成两种。** 这是 computer 那一页当初较过真的同一件事：
 *
 * - `503` —— 浏览器服务没在跑。给出把它跑起来的命令。
 * - `204` —— 在跑，但还没打开过任何页面。这不是错误，是"等一个动作"。
 * - `200` —— 有帧。
 *
 * 把前两种画成同一个空框，读者就没法知道该去启动一个进程，还是该等模型动手。
 *
 * 轮询而不是推流：ADR-095 立下的形状，这里照搬。人要看的是"模型现在在看
 * 什么"，不是流畅回放，为此新开一条 WebSocket 通道等于给这套东西加第二种
 * 实时范式，而已有的那一种服务的正是同一形状的需求。
 */

import { useEffect, useRef, useState } from "react";
import { fetchBrowserFrame } from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";

/** 两次取帧之间隔多久。 */
const POLL_MS = 1000;

type Frame =
  | { kind: "loading" }
  | { kind: "absent" }
  | { kind: "idle" }
  | { kind: "shown"; url: string };

interface Props {
  /**
   * 谁在看。帧那条路由和其他每一条一样先认身份头（ADR-044），第一版这里裸
   * `fetch` 不带头，在 Compose 栈上一秒一次 401，面板永远说「没在跑」。
   */
  identity: PrincipalIdentity;
}

export function BrowserFrame({ identity }: Props) {
  const [frame, setFrame] = useState<Frame>({ kind: "loading" });
  // 上一帧的 object URL，拿到新的之后要撤销——一秒一张，不撤销就是一分钟六十
  // 个 blob 挂在文档上。
  const previous = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const revoke = () => {
      if (previous.current !== null) {
        URL.revokeObjectURL(previous.current);
        previous.current = null;
      }
    };

    const tick = async () => {
      try {
        const response = await fetchBrowserFrame(identity);
        if (cancelled) return;
        if (response.status === 204) {
          revoke();
          setFrame({ kind: "idle" });
          return;
        }
        if (!response.ok) {
          revoke();
          setFrame({ kind: "absent" });
          return;
        }
        const blob = await response.blob();
        if (cancelled) {
          return;
        }
        const url = URL.createObjectURL(blob);
        revoke();
        previous.current = url;
        setFrame({ kind: "shown", url });
      } catch {
        // 取不到和没在跑，对读者是同一件事：这里看不到那个浏览器。
        if (!cancelled) {
          revoke();
          setFrame({ kind: "absent" });
        }
      }
    };

    void tick();
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      revoke();
    };
  }, [identity]);

  if (frame.kind === "loading") {
    return <p className="aw-code-workspace-empty">正在取画面……</p>;
  }

  if (frame.kind === "absent") {
    return (
      <div className="aw-code-workspace-empty">
        <p>浏览器服务没有在这套部署里应答。</p>
        <p>
          原生路径：<code>scripts/dev.sh browser-server</code>
          ；容器里它是 <code>browser</code> 服务。
        </p>
      </div>
    );
  }

  if (frame.kind === "idle") {
    return (
      <p className="aw-code-workspace-empty">
        浏览器在跑，但还没打开过页面。模型调用 <code>browser_open</code>{" "}
        之后这里就会有画面。
      </p>
    );
  }

  return (
    <div className="aw-browser-frame">
      <img alt="浏览器当前画面" src={frame.url} />
      <p className="aw-code-value">
        只读：这里看得到模型在做什么，但点不动它。
      </p>
    </div>
  );
}
