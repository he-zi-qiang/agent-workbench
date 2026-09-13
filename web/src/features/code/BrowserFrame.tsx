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

import type React from "react";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  fetchBrowserFrame,
  sendBrowserInput,
  type BrowserInputAction,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";

/**
 * 两次取帧之间隔多久：没人在操作时一秒一张（ADR-095 的形状）；读者把焦点放进
 * 画面之后四张一秒——一个在按方向键的人要看见自己按下去的结果，一秒一张等于
 * 每一步都在猜（ADR-0117）。
 */
const POLL_MS = 1000;
const POLL_MS_FOCUSED = 250;
/**
 * 一句拒绝挂多久。第一版一直挂到下一次输入，而实测里模型那一轮 11 秒就结束了，
 * 「模型正在操作这个页面」却留在那里——读者看到的是「还是不能操作」，正是这块
 * 面要消掉的那句话。四秒够读完，之后退回平常的提示；再点一下当然又会得到
 * 当时的真实答案。
 */
const REFUSAL_MS = 4000;

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
  /** 拒绝那句话挂多久；测试里传得短，产品里不传。 */
  refusalMs?: number;
}

export function BrowserFrame({ identity, refusalMs = REFUSAL_MS }: Props) {
  const [frame, setFrame] = useState<Frame>({ kind: "loading" });
  // 上一帧的 object URL，拿到新的之后要撤销——一秒一张，不撤销就是一分钟六十
  // 个 blob 挂在文档上。
  const previous = useRef<string | null>(null);
  // 读者的焦点在不在画面里（决定取帧的节奏，也决定滚轮归谁），一次输入是不是
  // 还在路上，以及服务端上一次为什么拒绝——模型在跑的那一轮里它答 409，那句话
  // 要画出来，而不是让点击悄悄丢掉（ADR-0117）。
  const [focused, setFocused] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const send = async (actions: readonly BrowserInputAction[]) => {
    if (busy) return;
    setBusy(true);
    try {
      await sendBrowserInput(identity, actions);
      setRefusal(null);
    } catch (cause) {
      setRefusal(
        cause instanceof ApiError && cause.status === 409
          ? "模型正在操作这个页面；等这一轮结束再点。"
          : cause instanceof ApiError && cause.status === 403
            ? "这个身份没有 mcp:browser，动不了它。"
            : "这一次输入没送进去。",
      );
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (refusal === null) return;
    const timer = window.setTimeout(() => setRefusal(null), refusalMs);
    return () => window.clearTimeout(timer);
  }, [refusal, refusalMs]);

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
    const timer = window.setInterval(
      () => void tick(),
      focused ? POLL_MS_FOCUSED : POLL_MS,
    );
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      revoke();
    };
  }, [focused, identity]);

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
      {/* 可以点、可以按键（ADR-0117）。`role="application"`：这一块吞掉方向键
          和空格，读屏器的表格导航在它里面不成立，这是那个 role 的本意。坐标从
          画面像素换算到浏览器视口像素——画面就是视口的截图，所以是等比。 */}
      <div
        aria-label="浏览器画面，点击或按键会送进模型的浏览器"
        className={`aw-browser-stage${busy ? " is-busy" : ""}`}
        onBlur={() => setFocused(false)}
        onClick={(event) => {
          const image = event.currentTarget.querySelector("img");
          if (image === null) return;
          const rect = image.getBoundingClientRect();
          const scaleX = (image.naturalWidth || VIEWPORT.width) / (rect.width || VIEWPORT.width);
          const scaleY =
            (image.naturalHeight || VIEWPORT.height) / (rect.height || VIEWPORT.height);
          event.currentTarget.focus();
          void send([
            {
              kind: "click",
              x: Math.max(0, Math.round((event.clientX - rect.left) * scaleX)),
              y: Math.max(0, Math.round((event.clientY - rect.top) * scaleY)),
            },
          ]);
        }}
        onFocus={() => setFocused(true)}
        onKeyDown={(event) => {
          const key = keyNameOf(event);
          if (key === null) return;
          event.preventDefault();
          void send([{ kind: "key", text: key }]);
        }}
        onWheel={(event) => {
          if (!focused) return;
          void send([{ kind: "scroll", delta_y: Math.round(event.deltaY) }]);
        }}
        role="application"
        tabIndex={0}
      >
        <img alt="浏览器当前画面" src={frame.url} />
      </div>
      <p className="aw-code-value" role="status">
        {refusal ??
          (focused
            ? "键盘和点击正送进模型的浏览器。"
            : "点一下画面就能操作它；模型在跑的那一轮里它不接受。")}
      </p>
    </div>
  );
}

/**
 * 浏览器那一侧视口的大小（`apps/browser_mcp/session.py` 的 `VIEWPORT`），只在画面
 * 还没量出自己多大时当兜底用——jsdom 下永远是这种情况。
 */
const VIEWPORT = { width: 1280, height: 800 } as const;

/**
 * 一次键盘事件对应到 Playwright 的键名，送不进去的返回 null。
 *
 * 单字符原样送（字母、数字、标点），空格送 `Space`，方向键、回车、Esc、Tab、退格、
 * 删除照 `KeyboardEvent.key` 的拼法——Playwright 认的正是这一套。带 Ctrl/Meta 的
 * 组合不送：那多半是读者自己浏览器的快捷键（刷新、复制），不该被吞。
 */
function keyNameOf(event: React.KeyboardEvent): string | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  const key = event.key;
  if (key === " ") return "Space";
  if (key.length === 1) return key;
  const named = new Set([
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "Home",
    "End",
    "PageUp",
    "PageDown",
  ]);
  return named.has(key) ? key : null;
}
