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
 * 因为「这是哪个文件」是读者问这块面的第一个问题。它在项目目录里的时候，那一行
 * 旁边有「在文件夹中打开」，反方向是右栏里 `.html` 文件的「在浏览器中打开」——
 * 文件夹和浏览器是一件事的两面（ADR-0120）。
 *
 * **按住就是按住（ADR-0120）。** 按下送 `key_down`、抬起送 `key_up`，鼠标同理。
 * 第一版每个键送一次 `key`——浏览器那一侧是 `keyboard.press`，按下和抬起一口气
 * 做完——而马里奥每一帧读一次 `keys.right`：keydown 置真、keyup 置假，两者之间
 * 一帧都没跑，于是人按住方向键，页面里的角色一步也不走。用户的原话是「人还是
 * 无法控制在浏览器中的项目预览」。输入**排队按顺序送**、不再在上一次没回来时丢
 * 掉：丢掉一个 `key_up` 就是一个松不开的键。失去焦点时把还按着的全部松开。
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
 * 画面之后十张一秒。
 *
 * 焦点下原来是 250 ms（ADR-0117），够看一次点击的结果，不够操作：人按住方向键
 * 走一段，每秒只看见四个位置，停在哪儿全靠猜（ADR-0120）。100 ms 是「看得见
 * 自己在控制」的下限；一帧是回环上几十 KB 的 JPEG。取帧改成上一张回来之后才
 * 排下一张，不再用 `setInterval`——间隔一短，慢的那一次就会和下一次叠在一起。
 */
const POLL_MS = 1000;
const POLL_MS_FOCUSED = 100;
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
  /**
   * 这段会话的项目目录，服务端看到的那个路径（例如 `/projects/windows测试`）。
   * 浏览器容器把同一个文件夹挂在同一个路径上，所以画面地址落在它下面时，那
   * 就是项目里的一个文件，可以回到文件夹里打开（ADR-0120）。
   */
  projectRoot?: string | null | undefined;
  /** 在文件夹那一张里打开项目内的一个相对路径。 */
  onRevealFile?: ((path: string) => void) | undefined;
}

/**
 * 画面地址落在项目目录下时，它在项目里的相对路径；否则 null。
 *
 * 只认目录**之下**的：根本身不是一个能打开的文件，只有前缀相同的另一个目录
 * （`/projects/demo2` 之于 `/projects/demo`）也不算。
 */
export function relativeToProject(
  where: string | null,
  projectRoot: string | null | undefined,
): string | null {
  if (where === null || projectRoot == null || projectRoot === "") return null;
  const root = projectRoot.endsWith("/") ? projectRoot : `${projectRoot}/`;
  if (!where.startsWith(root)) return null;
  const relative = where.slice(root.length).split(/[?#]/)[0] ?? "";
  return relative === "" ? null : relative;
}

/** 一次请求最多带几个动作，和服务端 `MAX_ACTIONS` 同一个数。 */
const MAX_ACTIONS = 10;

export function BrowserFrame({
  identity,
  onRevealFile,
  projectRoot,
  refusalMs = REFUSAL_MS,
}: Props) {
  const [frame, setFrame] = useState<Frame>({ kind: "loading" });
  // 上一帧的 object URL，拿到新的之后要撤销——一秒一张，不撤销就是一分钟六十
  // 个 blob 挂在文档上。
  const previous = useRef<string | null>(null);
  // 读者的焦点在不在画面里（决定取帧的节奏，也决定滚轮归谁），一次输入是不是
  // 还在路上，以及上一次送进去之后要说的那句话——它现在只有两种来源：真的出错
  // （403、网络），或者「模型这一轮也在动这个页面」这个事实（ADR-0119）。
  const [focused, setFocused] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  // 送出去的输入排成一条队，一个回来了才送下一个（ADR-0120）。第一版在上一次
  // 没回来时直接丢掉新的——对轻点无所谓，对按下/抬起是灾难：丢掉的若是
  // `key_up`，浏览器那边这个键就一直按着。
  const queue = useRef<Promise<void>>(Promise.resolve());
  // 此刻按着的键、按着的鼠标（和它最后在哪）。失去焦点、卸载时靠它们把手松开。
  const heldKeys = useRef<Set<string>>(new Set());
  const heldMouse = useRef<{ x: number; y: number } | null>(null);
  // 还没送出去的那一次移动。拖动时鼠标事件比一次往返快得多，排着的移动只保留
  // 最新的位置，否则松手之后画面还要追着一串旧坐标走。
  const pendingMove = useRef<{ x: number; y: number } | null>(null);

  const deliver = async (actions: readonly BrowserInputAction[]) => {
    try {
      const answered = await sendBrowserInput(identity, actions);
      // 不是拒绝，是提醒：输入已经进去了，只是模型这一轮也在动这一页，画面可能
      // 会在人的手底下变（ADR-0119）。
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
    }
  };
  const send = (actions: readonly BrowserInputAction[]) => {
    for (let start = 0; start < actions.length; start += MAX_ACTIONS) {
      const batch = actions.slice(start, start + MAX_ACTIONS);
      queue.current = queue.current.then(() => deliver(batch));
    }
  };
  const move = (point: { x: number; y: number }) => {
    const alreadyQueued = pendingMove.current !== null;
    pendingMove.current = point;
    if (alreadyQueued) return;
    queue.current = queue.current.then(async () => {
      const latest = pendingMove.current;
      pendingMove.current = null;
      if (latest !== null) await deliver([{ kind: "mouse_move", ...latest }]);
    });
  };
  // 把还按着的都松开。挂在失去焦点上：读者按住方向键时点了一下别处，抬起那一
  // 下发生在这块面之外、它听不到——不补这一下，角色就一直往右走。
  const releaseHeld = () => {
    const actions = releasing(heldKeys.current, heldMouse.current);
    heldKeys.current.clear();
    heldMouse.current = null;
    if (actions.length > 0) send(actions);
  };
  // 卸载时同一件事（切到别的标签、关掉右栏）。写在 effect 的清理里、只碰 ref：
  // 这时组件已经不在了，不能再设状态，送出去的只管送到，不再管回来的是什么。
  const identityRef = useRef(identity);
  useEffect(() => {
    identityRef.current = identity;
  }, [identity]);
  useEffect(() => {
    const keys = heldKeys;
    const mouse = heldMouse;
    const pending = queue;
    const who = identityRef;
    return () => {
      const actions = releasing(keys.current, mouse.current);
      keys.current.clear();
      mouse.current = null;
      if (actions.length === 0) return;
      pending.current = pending.current.then(() =>
        sendBrowserInput(who.current, actions.slice(0, MAX_ACTIONS)).then(
          () => undefined,
          () => undefined,
        ),
      );
    };
  }, []);

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

    // 上一张回来之后才排下一张（ADR-0120）：100 ms 的节奏下，一次慢的取帧和
    // 下一次叠在一起，画面会倒着跳。
    let timer: number | undefined;
    const loop = async () => {
      await tick();
      if (!cancelled) {
        timer = window.setTimeout(
          () => void loop(),
          focused ? POLL_MS_FOCUSED : POLL_MS,
        );
      }
    };
    void loop();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
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
        之后这里就会有画面；你也可以在「文件夹」里点开一个 <code>.html</code>{" "}
        文件，用「在浏览器中打开」把它放到这里。
      </p>
    );
  }

  const revealable = relativeToProject(frame.where, projectRoot);

  return (
    <div className="aw-browser-frame">
      {/* 可以点、可以按键（ADR-0117）。`role="application"`：这一块吞掉方向键
          和空格，读屏器的表格导航在它里面不成立，这是那个 role 的本意。坐标从
          画面像素换算到浏览器视口像素——画面就是视口的截图，所以是等比。 */}
      <div
        aria-label="浏览器画面，点击或按键会送进模型的浏览器"
        className="aw-browser-stage"
        onBlur={() => {
          setFocused(false);
          releaseHeld();
        }}
        onFocus={() => setFocused(true)}
        onKeyDown={(event) => {
          // 带 Ctrl/Meta/Alt 的不送：那多半是读者自己浏览器的快捷键。
          if (event.ctrlKey || event.metaKey || event.altKey) return;
          const key = keyNameOf(event.key);
          if (key === null) return;
          event.preventDefault();
          // 按住时浏览器会不停地发 keydown（`repeat`）。那一侧的键已经按下了，
          // 再送一次按下不会让页面多知道什么，只会把队排长。
          if (event.repeat || heldKeys.current.has(key)) return;
          heldKeys.current.add(key);
          send([{ kind: "key_down", text: key }]);
        }}
        onKeyUp={(event) => {
          // 不看修饰键：按住方向键时按了一下 Ctrl，抬起那一下照样要送到，否则
          // 这个键在浏览器那边就松不开了。
          const key = keyNameOf(event.key);
          if (key === null || !heldKeys.current.has(key)) return;
          event.preventDefault();
          heldKeys.current.delete(key);
          send([{ kind: "key_up", text: key }]);
        }}
        onMouseDown={(event) => {
          if (event.button !== 0) return;
          const point = viewportPoint(event);
          if (point === null) return;
          event.preventDefault();
          event.currentTarget.focus();
          heldMouse.current = point;
          send([{ kind: "mouse_down", ...point }]);
        }}
        onMouseLeave={(event) => {
          // 按着拖出了画面：在离开的地方松开，不让浏览器那边一直按着。
          if (heldMouse.current === null) return;
          const point = viewportPoint(event) ?? heldMouse.current;
          heldMouse.current = null;
          send([{ kind: "mouse_up", ...point }]);
        }}
        onMouseMove={(event) => {
          if (heldMouse.current === null) return;
          const point = viewportPoint(event);
          if (point === null) return;
          heldMouse.current = point;
          move(point);
        }}
        onMouseUp={(event) => {
          if (heldMouse.current === null) return;
          const point = viewportPoint(event) ?? heldMouse.current;
          heldMouse.current = null;
          send([{ kind: "mouse_up", ...point }]);
        }}
        onWheel={(event) => {
          if (!focused) return;
          send([{ kind: "scroll", delta_y: Math.round(event.deltaY) }]);
        }}
        role="application"
        tabIndex={0}
      >
        {/* 不许原生拖图：按住画面拖动是送进浏览器的拖动，不是把这张 JPEG 拖走。 */}
        <img
          alt="浏览器当前画面"
          draggable={false}
          onDragStart={(event) => event.preventDefault()}
          src={frame.url}
        />
      </div>
      {frame.where !== null && (
        <p className="aw-browser-where" title={frame.where}>
          <span>{frame.where}</span>
          {revealable !== null && onRevealFile !== undefined && (
            <button
              className="aw-browser-reveal"
              onClick={() => onRevealFile(revealable)}
              type="button"
            >
              在文件夹中打开
            </button>
          )}
        </p>
      )}
      <p className="aw-code-value" role="status">
        {refusal ??
          (focused
            ? "键盘和鼠标正送进模型的浏览器：按住就是按住。"
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

/** 松开此刻按着的一切要送的那几个动作：每个键一个 `key_up`，鼠标一个 `mouse_up`。 */
function releasing(
  keys: ReadonlySet<string>,
  mouse: { x: number; y: number } | null,
): BrowserInputAction[] {
  const actions: BrowserInputAction[] = [...keys].map((text) => ({
    kind: "key_up",
    text,
  }));
  if (mouse !== null) actions.push({ kind: "mouse_up", ...mouse });
  return actions;
}

/**
 * 画面上一次鼠标事件落在浏览器视口的哪个像素上。
 *
 * 画面就是视口的截图，所以是等比换算；图还没量出自己多大时（jsdom 下永远是）
 * 退回视口的尺寸，比例为 1。
 */
function viewportPoint(
  event: React.MouseEvent<HTMLDivElement>,
): { x: number; y: number } | null {
  const image = event.currentTarget.querySelector("img");
  if (image === null) return null;
  const rect = image.getBoundingClientRect();
  const scaleX = (image.naturalWidth || VIEWPORT.width) / (rect.width || VIEWPORT.width);
  const scaleY =
    (image.naturalHeight || VIEWPORT.height) / (rect.height || VIEWPORT.height);
  return {
    x: Math.max(0, Math.round((event.clientX - rect.left) * scaleX)),
    y: Math.max(0, Math.round((event.clientY - rect.top) * scaleY)),
  };
}

/**
 * `KeyboardEvent.key` 对应到 Playwright 的键名，送不进去的返回 null。
 *
 * 单字符原样送（字母、数字、标点），空格送 `Space`，方向键、回车、Esc、Tab、退格、
 * 删除照 `KeyboardEvent.key` 的拼法——Playwright 认的正是这一套。按下和抬起用同一
 * 个名字，所以按住期间按了 Shift 让 `key` 从 `a` 变成 `A` 的那种情形，抬起时对不上
 * 就不送；失去焦点时 `releaseAll` 兜底。
 */
function keyNameOf(key: string): string | null {
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
