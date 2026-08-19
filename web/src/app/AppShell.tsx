import {
  Activity,
  Code2,
  FlaskConical,
  Library,
  MonitorCog,
  MonitorSmartphone,
  Moon,
  MoreHorizontal,
  MessageSquare,
  Settings2,
  Sun,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { EnvironmentDialog } from "./EnvironmentDialog";
import { useIdentity } from "./IdentityContext";
import { NEXT_MODE, type ThemeMode, useTheme } from "./ThemeContext";

const NAVIGATION = [
  // One entry for two routes. `covers` is what marks it current on `/work`
  // too -- a `NavLink to="/chat"` is not, and a rail item that goes dark the
  // moment you open the 任务 tab is a rail that disagrees with the page.
  {
    to: "/chat",
    label: "工作台",
    icon: MessageSquare,
    primary: true,
    covers: ["/chat", "/work"],
  },
  { to: "/code", label: "Code", icon: Code2, primary: true, covers: ["/code"] },
  {
    to: "/knowledge",
    label: "知识库",
    icon: Library,
    primary: false,
    covers: ["/knowledge"],
  },
  {
    to: "/evaluation",
    label: "效果评测",
    icon: FlaskConical,
    primary: false,
    covers: ["/evaluation"],
  },
  {
    to: "/computer",
    label: "计算机",
    icon: MonitorSmartphone,
    primary: false,
    covers: ["/computer"],
  },
  {
    to: "/system",
    label: "运行状态",
    icon: Activity,
    primary: false,
    covers: ["/system"],
  },
] as const;

/**
 * Where the rail's one dividing line goes: before the first entry that is not
 * a primary flow.
 *
 * Derived rather than written as an index. It used to be `index === 1`, which
 * put the line above Code and so drew 工作台 and Code as two separate groups --
 * the opposite of what they are. Deriving it from `primary` also removes the
 * failure mode that made the hardcoded version fragile: reordering NAVIGATION
 * moved the entries and left the line where it was.
 */
const FIRST_SECONDARY_INDEX = NAVIGATION.findIndex((item) => !item.primary);

/**
 * 主题按钮显示的是**当前这一档**，不是"点了会变成什么"。
 *
 * 两种写法都常见，但只有一种在三档下不会说谎：显示"下一档"的按钮在 system 档上
 * 必须画一个太阳（下一档是浅色），而此刻屏幕可能正是深的——图标于是和眼前的
 * 界面相反。显示当前档就没有这个问题，代价是要有 title 说清点下去会怎样。
 */
const THEME_LABEL: Record<ThemeMode, { icon: typeof Sun; text: string }> = {
  system: { icon: MonitorCog, text: "跟随系统" },
  light: { icon: Sun, text: "浅色" },
  dark: { icon: Moon, text: "深色" },
};

function ThemeControl() {
  const { mode, resolved, cycleMode } = useTheme();
  const { icon: Icon, text } = THEME_LABEL[mode];
  // `system` 档下补一句当前解析成了什么。这一档的按钮文字说的是"跟随"，而跟随
  // 的结果是屏幕现在的样子——不说出来的话，唯一能回答"现在到底是深还是浅"的
  // 东西就是眼睛，而这正是有人来点这个按钮的原因。
  const current = mode === "system" ? `（当前${resolved === "dark" ? "深色" : "浅色"}）` : "";
  return (
    <button
      className="aw-global-link aw-theme-button"
      onClick={cycleMode}
      title={`主题：${text}${current} · 点击切换到${THEME_LABEL[NEXT_MODE[mode]].text}`}
      type="button"
    >
      <Icon aria-hidden="true" size={18} />
      <span>{text}</span>
    </button>
  );
}

export function AppShell() {
  const { identity, setEditorOpen } = useIdentity();
  const { mode: themeMode, cycleMode: cycleTheme } = useTheme();
  const ThemeIcon = THEME_LABEL[themeMode].icon;
  const location = useLocation();
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  // Everything that is not a primary flow and not already on the mobile bar.
  // Derived rather than an explicit pair of paths: the hardcoded version left
  // a newly added secondary page reachable on desktop and nowhere on mobile.
  const secondaryNavigation = NAVIGATION.filter(
    (item) => !item.primary && item.to !== "/knowledge",
  );
  const secondaryActive = secondaryNavigation.some((item) =>
    location.pathname.startsWith(item.to),
  );
  const identityKey = JSON.stringify([
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort(),
  ]);
  return (
    <div className="aw-app-shell">
      <nav className="aw-global-rail" aria-label="主导航">
        <NavLink aria-label="Agent Workbench" className="aw-logo" to="/chat">
          A
        </NavLink>
        {NAVIGATION.map((item, index) => {
          const Icon = item.icon;
          const current = item.covers.some((prefix) =>
            location.pathname.startsWith(prefix),
          );
          return (
            <div
              className={index === FIRST_SECONDARY_INDEX ? "aw-nav-divider" : ""}
              key={item.to}
            >
              {/* `Link`, not `NavLink`: this entry stands for a set of
                  prefixes, and `NavLink` overwrites `aria-current` with its own
                  single-path match -- which reads "not here" on /work. */}
              <Link
                aria-current={current ? "page" : undefined}
                className={`aw-global-link ${current ? "active" : ""}`}
                title={item.label}
                to={item.to}
              >
                <Icon aria-hidden="true" size={18} />
                <span>{item.label}</span>
              </Link>
            </div>
          );
        })}
        <div className="aw-rail-spacer" />
        <ThemeControl />
        <button
          className="aw-global-link aw-env-button"
          onClick={() => setEditorOpen(true)}
          title="本地环境"
          type="button"
        >
          <Settings2 aria-hidden="true" size={18} />
          <span>环境</span>
        </button>
      </nav>
      {/* 右上角常驻的身份。此前只有一句 sr-only 和「环境」按钮后面的对话框，
          于是屏幕上任何一个数字都答不出"这是以谁的身份取到的"——而这个控制台的
          每一页内容都取决于它。点它就是打开那个对话框，不另开一个入口。 */}
      <button
        className="aw-identity-pill"
        onClick={() => setEditorOpen(true)}
        title="点击编辑本地身份"
        type="button"
      >
        <span>{identity.tenantId}</span>
        <span aria-hidden="true">/</span>
        <span>{identity.principalId}</span>
      </button>
      <section className="aw-app-content">
        {/* Remount every routed surface at an identity boundary so an old
            principal's authorized projection cannot remain on screen. */}
        <Outlet key={identityKey} />
      </section>
      <nav className="aw-mobile-nav" aria-label="移动端导航">
        {NAVIGATION.filter((item) => item.primary || item.to === "/knowledge").map(
          (item) => {
            const Icon = item.icon;
            const current = item.covers.some((prefix) =>
              location.pathname.startsWith(prefix),
            );
            return (
              <Link
                aria-current={current ? "page" : undefined}
                className={`aw-mobile-link ${current ? "active" : ""}`}
                key={item.to}
                to={item.to}
              >
                <Icon aria-hidden="true" size={19} />
                <span>{item.label}</span>
              </Link>
            );
          },
        )}
        <button
          aria-expanded={mobileMoreOpen}
          aria-haspopup="dialog"
          className={`aw-mobile-link ${secondaryActive ? "active" : ""}`}
          onClick={() => setMobileMoreOpen(true)}
          type="button"
        >
          <MoreHorizontal aria-hidden="true" size={19} />
          <span>更多</span>
        </button>
      </nav>
      {mobileMoreOpen ? (
        <div
          className="aw-mobile-more-backdrop"
          onClick={() => setMobileMoreOpen(false)}
          role="presentation"
        >
          <section
            aria-label="更多页面"
            aria-modal="true"
            className="aw-mobile-more-sheet"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <header>
              <div>
                <span className="aw-eyebrow">更多</span>
                <h2>项目工具</h2>
              </div>
              <button
                aria-label="关闭更多页面"
                className="aw-icon-button"
                onClick={() => setMobileMoreOpen(false)}
                type="button"
              >
                <X aria-hidden="true" size={18} />
              </button>
            </header>
            <nav aria-label="更多项目页面" className="aw-mobile-more-list">
              {secondaryNavigation.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    className="aw-mobile-more-link"
                    key={item.to}
                    onClick={() => setMobileMoreOpen(false)}
                    to={item.to}
                  >
                    <Icon aria-hidden="true" size={19} />
                    <span>{item.label}</span>
                  </NavLink>
                );
              })}
              {/* 主题也在这里出现一次：rail 在 760px 以下是隐藏的，而它是
                  主题按钮唯一的入口，只放在 rail 上等于移动端没有主题开关。
                  这一项不关闭面板——连点三下看三档，比每点一次都要重新打开
                  「更多」要合理。 */}
              <button
                className="aw-mobile-more-link"
                onClick={cycleTheme}
                type="button"
              >
                <ThemeIcon aria-hidden="true" size={19} />
                <span>主题：{THEME_LABEL[themeMode].text}</span>
              </button>
              <button
                className="aw-mobile-more-link"
                onClick={() => {
                  setMobileMoreOpen(false);
                  setEditorOpen(true);
                }}
                type="button"
              >
                <Settings2 aria-hidden="true" size={19} />
                <span>本地环境与身份</span>
              </button>
            </nav>
          </section>
        </div>
      ) : null}
      <span className="aw-sr-only">
        当前本地身份：{identity.tenantId} / {identity.principalId}
      </span>
      <EnvironmentDialog />
    </div>
  );
}
