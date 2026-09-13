/**
 * 模型此刻在浏览器里看到的那一帧（ADR-0113 §3.6）。
 *
 * **它能操作，而且什么时候都能（ADR-0117 起，仲裁改于 ADR-0119）。** 点一下把
 * 视口坐标送进去，焦点在里面时按键和滚轮也送。ADR-0117 的第一版只在没有回合
 * 在跑的时候接受，而一个「写完页面再去浏览器里验」的回合要跑几分钟——人想操作
 * 的正是那几分钟，于是这块面在它唯一有用的时刻永远答「等这一轮结束」。现在两
 * 边都能动，模型在它下一次浏览器调用上被告知页面被人碰过。
 *
 * **画面底下有一行地址**：浏览器所在那台机器上的路径（容器栈里是容器里的），
 * 因为「这是哪个文件」是读者问这块面的第一个问题。
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
  | { kind: "shown"; url: string; where: string | null };

/** 帧那条路由把页面自己的地址带在帧上（ADR-0119）。 */
const URL_HEADER = "X-Browser-Url";

/**
 * 把浏览器报的地址变成人能认的一行。
 *
 * `file://` 的那一半是这件事的重点：模型验证自己刚写的文件时打开的就是它，而
 * 那是**浏览器所在那台机器上**的路径——容器栈里是容器里的路径，原生路径上就是
 * 本机的。读者问这块面的第一个问题是「这是哪个文件」，在此之前它答不上来。
 * 百分号编码要解开：`/projects/windows%E6%B5%8B%E8%AF%95/mario.html` 谁也认不出。
 */
export function whereItIs(raw: string | null): string | null {
  if (raw === null || raw === "") return null;
  let text = raw;
  try {
    text = decodeURIComponent(raw);
  } catch {
    // 解不开就照原样显示：一个地址总比没有强。
  }
  return text.startsWith("file://") ? text.slice("file://".length) : text;
}

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
  // 还在路上，以及上一次送进去之后要说的那句话——它现在只有两种来源：真的出错
  // （403、网络），或者「模型这一轮也在动这个页面」这个事实（ADR-0119）。
  const [focused, setFocused] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const send = async (actions: readonly BrowserInputAction[]) => {
    if (busy) return;
    setBusy(true);
    try {
      const answered = await sendBrowserInput(identity, actions);
      // 不是拒绝，是提醒：输入已经进去了，只是模型这一轮也在动这一页，画面可能
      // 会在人的手底下变。ADR-0117 在这里答 409 并且什么也不做，而一轮要跑几
      // 分钟——读者看到的就是「浏览器一直被控制，人动不了」。
      setRefusal(
        answered.turns_in_flight > 0
          ? "送进去了。模型这一轮也在动这个页面，画面可能会自己变。"
          : null,
      );
    } catch (cause) {
      setRefusal(
        cause instanceof ApiError && cause.status === 403
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
        // 读在 `blob()` 之前：地址和它描述的那张图是同一次应答带来的，隔一次
        // 轮询去取就可能配成另一页的地址（ADR-0119）。
        const where = whereItIs(response.headers.get(URL_HEADER));
        const blob = await response.blob();
        if (cancelled) {
          return;
        }
        const url = URL.createObjectURL(blob);
        revoke();
        previous.current = url;
        setFrame({ kind: "shown", url, where });
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
      {frame.where !== null && (
        <p className="aw-browser-where" title={frame.where}>
          {frame.where}
        </p>
      )}
      <p className="aw-code-value" role="status">
        {refusal ??
          (focused
            ? "键盘和点击正送进模型的浏览器。"
            : "点一下画面就能操作它；模型在跑的时候也可以。")}
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
