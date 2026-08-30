import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { MonitorCog, Moon, Sun, type LucideIcon } from "lucide-react";
import { useStoredState } from "../hooks/useStoredState";

/**
 * 三个值，不是两个。
 *
 * `system` 不是"还没选"，它是一个可以被选中的选项，而且是默认值。这三档和 CSS
 * 那边的三条规则一一对应（tokens.css 末尾）：`system` 不写 `data-theme`，让
 * `:root { color-scheme: light dark }` 去问系统；另外两档写死属性压过系统。
 *
 * 被拒绝的设计是两档布尔（浅/深）加一个"跟随系统"的复选框：那样"当前是深色"和
 * "深色是我选的"变成两个状态，而它们会不同步——系统切到深色时，一个选了浅色的
 * 用户的复选框该显示什么，没有正确答案。
 */
export const THEME_MODES = ["system", "light", "dark"] as const;

/**
 * 每一档在屏幕上叫什么、配哪个图标。
 *
 * 和 `THEME_MODES` 住在一起，因为凡是加一档的人必须同时给它一个名字——分成两个
 * 文件的话，漏掉的那半在运行时是 `undefined.icon`，而它只在读者真的切到那一档时
 * 才炸。
 */
export const THEME_LABEL: Record<ThemeMode, { icon: LucideIcon; text: string }> =
  {
    system: { icon: MonitorCog, text: "跟随系统" },
    light: { icon: Sun, text: "浅色" },
    dark: { icon: Moon, text: "深色" },
  };
export type ThemeMode = (typeof THEME_MODES)[number];

/**
 * 点一下之后是哪一档。
 *
 * 写成映射而不是 `THEME_MODES[(i + 1) % 3]`：后者要么在 `noUncheckedIndexedAccess`
 * 下多一个不可能发生的 undefined 分支，要么靠一句 `!` 把它按下去。更重要的是
 * 顺序在这里只写一次——rail 上那个 "点击切换到 X" 的提示直接读这张表，不必自己
 * 再排一遍同样的顺序（排两遍的顺序会在有人插入第四档时分叉）。
 */
export const NEXT_MODE: Record<ThemeMode, ThemeMode> = {
  system: "light",
  light: "dark",
  dark: "system",
};

/** 浏览器地址栏/状态栏的取色，两个主题各一个。与 tokens.css 的 --aw-canvas 同值。 */
const THEME_COLOR: Record<"light" | "dark", string> = {
  light: "#f2f0eb",
  dark: "#171614",
};

export const THEME_STORAGE_KEY = "aw.theme.v1";

interface ThemeContextValue {
  /** 用户选的那一档。`system` 时不代表当前深浅。 */
  mode: ThemeMode;
  /** 当前**实际**在用的深浅。`system` 时由系统偏好解析而来。 */
  resolved: "light" | "dark";
  setMode: (mode: ThemeMode) => void;
  /** 在三档之间前进一格：system → light → dark → system。 */
  cycleMode: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * 读系统偏好。
 *
 * 单独抽出来是因为它有两处会失败：jsdom 里 `matchMedia` 不存在（测试环境），
 * 老 Safari 里它存在但没有 `addEventListener`。两处都不该让控制台白屏，所以
 * 读不到就按浅色算——和 `color-scheme: light dark` 里 `light` 在前是同一个默认。
 */
function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/**
 * localStorage 里存的东西不受这个文件管：它可能是上一版写的档位名，也可能是有人
 * 手输进去的。`useStoredState` 直接 `JSON.parse` 后按 T 交出来，类型标注在这里
 * 是一句没有被验证的断言。
 *
 * 不认识就落回 `system`，而不是原样传下去——原样传下去的后果是 `THEME_LABEL[mode]`
 * 取到 undefined、rail 上的按钮在渲染时炸掉，一个存量的脏字符串因此变成一块白屏。
 */
function normalizeMode(value: ThemeMode): ThemeMode {
  return (THEME_MODES as readonly string[]).includes(value) ? value : "system";
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [storedMode, setMode] = useStoredState<ThemeMode>(
    THEME_STORAGE_KEY,
    "system",
  );
  const mode = normalizeMode(storedMode);

  // 系统偏好是会变的（用户在 macOS 的外观设置里切一下就变），而且在 `system`
  // 档下它就是当前主题。所以它必须是 state 而不是渲染时读一次的值——只读一次
  // 的版本在系统切换时不会重渲染，`resolved` 会停在旧值上，主题按钮于是显示
  // 着"浅色"而屏幕已经是深的。
  const [systemDark, setSystemDark] = useState(systemPrefersDark);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => setSystemDark(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, [setSystemDark]);

  const resolved: "light" | "dark" =
    mode === "system" ? (systemDark ? "dark" : "light") : mode;

  // 属性写在 <html> 上，不是 <body>：tokens.css 的两条覆盖规则选的是 `:root`，
  // 而且 `color-scheme` 只有落在根元素上才会影响滚动条与原生控件。
  useEffect(() => {
    const root = document.documentElement;
    if (mode === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", mode);
    }
  }, [mode]);

  // theme-color 有两条 <meta>，按 prefers-color-scheme 分（见 index.html）——
  // 那一对负责"跟随系统"这一档，在 JS 跑起来之前就已经正确。这里做的是显式选择
  // 时把两条都改成同一个值：这样无论系统是什么，浏览器取到的都是用户选的那个。
  useEffect(() => {
    const metas = document.head.querySelectorAll<HTMLMetaElement>(
      'meta[name="theme-color"]',
    );
    metas.forEach((meta) => {
      const scheme = meta.media.includes("dark") ? "dark" : "light";
      meta.content = mode === "system" ? THEME_COLOR[scheme] : THEME_COLOR[resolved];
    });
  }, [mode, resolved]);

  const cycleMode = useCallback(() => {
    setMode((current) => NEXT_MODE[normalizeMode(current)]);
  }, [setMode]);

  const value = useMemo(
    () => ({ mode, resolved, setMode, cycleMode }),
    [mode, resolved, setMode, cycleMode],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
