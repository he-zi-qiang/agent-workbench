import {
  Activity,
  BadgeCheck,
  KeyRound,
  Palette,
  Wallet,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { PrincipalIdentity } from "../api/types";
import { IconButton } from "../components/ui";
import { ProviderKeyPanel } from "../features/system/ProviderKeyPanel";
import { HealthReport } from "../features/system/SystemPage";
import { UsageReport } from "../features/usage/UsagePage";
import { useIdentity } from "./IdentityContext";
import { THEME_LABEL, THEME_MODES, useTheme } from "./ThemeContext";

/**
 * 设置。一个左边分类、右边内容的对话框。
 *
 * **它替掉的是「本地身份模拟器」。** 那个框只做一件事——改三个 header——而它占着
 * 这个界面上唯一一个「设置」形状的位置：左下角那颗头像。于是这台部署所有其他可
 * 调的东西各自找了个别的地方住：主题在 rail 上一颗会循环三档的按钮里，用量是一
 * 条路由，运行状态是另一条。四件事，四个入口，没有一个地方能回答「这台东西都有
 * 什么可调的」。
 *
 * **分类是竖排的，不是标签页。** 右栏那两处用的是标签页，因为它们要在一栏宽度里
 * 挤下四五格；这里是一个宽对话框，竖排的分类既放得下说明文字，也留得下以后往下
 * 加。这是 Claude Desktop 那个设置窗的形状，抄的是它的**结构**（左类右容、每一
 * 类一屏），不是它的条目——它那些条目背后的能力这台部署一多半没有。
 *
 * **只装真能改或真能看的东西。** 一个设置面板最容易犯的错是把「以后想支持的」
 * 先画上：一个点了没反应的开关，读者读成的是坏了。所以这里四类，全部有后端或有
 * 本地状态撑着：外观（`ThemeContext`）、用量（`GET /v1/usage`）、本地身份（那三
 * 个 header）、运行状态（两个健康检查端点）。
 *
 * **打开时落在「本地身份」。** 左下角那颗头像此前打开的就是身份编辑框，而一次
 * 改版不该让一个用惯了的按钮换掉它的后果。其余几类就在旁边。
 */

interface Section {
  id: string;
  label: string;
  icon: LucideIcon;
  hint: string;
}

const SECTIONS: readonly Section[] = [
  { id: "identity", label: "本地身份", icon: BadgeCheck, hint: "这台环境用哪个身份发请求" },
  { id: "provider", label: "模型密钥", icon: KeyRound, hint: "这台部署用哪把 key 调模型" },
  { id: "appearance", label: "外观", icon: Palette, hint: "浅色、深色，还是跟着系统" },
  { id: "usage", label: "用量", icon: Wallet, hint: "钱和 token 花在哪了" },
  { id: "health", label: "运行状态", icon: Activity, hint: "本机这几个进程还在不在" },
];

export function SettingsDialog() {
  const { identity, updateIdentity, editorOpen, setEditorOpen } = useIdentity();
  if (!editorOpen) return null;
  return (
    <SettingsDialogContent
      identity={identity}
      // 每次打开都重挂：草稿状态不该跨越两次打开活着。上一版就是这么做的，
      // 理由没变。
      key={JSON.stringify(identity)}
      onClose={() => setEditorOpen(false)}
      onSave={(next) => {
        updateIdentity(next);
        setEditorOpen(false);
      }}
    />
  );
}

function SettingsDialogContent({
  identity,
  onClose,
  onSave,
}: {
  identity: PrincipalIdentity;
  onClose: () => void;
  onSave: (identity: PrincipalIdentity) => void;
}) {
  const [section, setSection] = useState("identity");
  const [draft, setDraft] = useState(identity);
  const dialogRef = useRef<HTMLElement>(null);
  const returnFocus = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const focusTarget = returnFocus.current;
    const firstField = dialogRef.current?.querySelector<HTMLElement>("input");
    firstField?.focus();
    return () => {
      window.requestAnimationFrame(() => focusTarget?.focus());
    };
  }, []);

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) return;
    // 焦点关在框里。选择器里加了 `a` 和 `[tabindex]`：这个框现在装得下用量那张
    // 表和运行状态那几条，里头不再只有 input 和 button——漏掉的那些会让 Tab 走
    // 到框外面去，而背景是 `inert` 的，于是焦点消失在一个看不见的地方。
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'input:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((node) => node.offsetParent !== null);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const save = () => {
    onSave({
      ...draft,
      tenantId: draft.tenantId.trim(),
      principalId: draft.principalId.trim(),
      scopes: draft.scopes.map((scope) => scope.trim()).filter(Boolean),
    });
  };

  return (
    <div className="aw-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-labelledby="settings-title"
        aria-modal="true"
        className="aw-dialog aw-settings"
        onKeyDown={handleDialogKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <header>
          <h2 id="settings-title">设置</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X aria-hidden="true" size={17} />
          </IconButton>
        </header>

        <div className="aw-settings-body">
          <nav aria-label="设置分类" className="aw-settings-nav">
            {SECTIONS.map((entry) => (
              <button
                aria-current={entry.id === section ? "true" : undefined}
                className={`aw-settings-nav-item${entry.id === section ? " is-on" : ""}`}
                key={entry.id}
                onClick={() => setSection(entry.id)}
                type="button"
              >
                <entry.icon aria-hidden="true" size={16} />
                <span>
                  <strong>{entry.label}</strong>
                  <small>{entry.hint}</small>
                </span>
              </button>
            ))}
          </nav>

          <div className="aw-settings-pane">
            {section === "identity" ? (
              <IdentitySection
                draft={draft}
                onSave={save}
                setDraft={setDraft}
              />
            ) : null}
            {section === "provider" ? <ProviderKeyPanel /> : null}
            {section === "appearance" ? <AppearanceSection /> : null}
            {section === "usage" ? <UsageReport /> : null}
            {section === "health" ? <HealthReport /> : null}
          </div>
        </div>
      </section>
    </div>
  );
}

function IdentitySection({
  draft,
  onSave,
  setDraft,
}: {
  draft: PrincipalIdentity;
  onSave: () => void;
  setDraft: (value: PrincipalIdentity) => void;
}) {
  return (
    <>
      <h3>本地身份模拟器</h3>
      <p className="aw-settings-lede">
        这些 Header 只用于 loopback 开发环境，不是生产登录。切换身份后，服务端会重新执行对象级授权，本地列表也会按身份隔离（ADR-044）。
      </p>
      <div className="aw-form-stack">
        <label>
          <span>Tenant</span>
          <input
            onChange={(event) =>
              setDraft({ ...draft, tenantId: event.target.value })
            }
            value={draft.tenantId}
          />
        </label>
        <label>
          <span>Principal</span>
          <input
            onChange={(event) =>
              setDraft({ ...draft, principalId: event.target.value })
            }
            value={draft.principalId}
          />
        </label>
        <label>
          <span>Scopes（逗号分隔）</span>
          <input
            onChange={(event) =>
              setDraft({ ...draft, scopes: event.target.value.split(",") })
            }
            value={draft.scopes.join(", ")}
          />
        </label>
      </div>
      {/* 「应用」贴着它改的那三个框，不在对话框页脚。
          页脚那个位置在一个分类式的设置面板里是有歧义的：它看起来在为**整个面板**
          负责，而它只能保存这一类里的东西——另外三类根本没有待保存的草稿（主题即点
          即生效，另外两类是只读的）。 */}
      <div className="aw-settings-actions">
        <button
          className="aw-button is-primary"
          disabled={!draft.tenantId.trim() || !draft.principalId.trim()}
          onClick={onSave}
          type="button"
        >
          应用身份
        </button>
      </div>
    </>
  );
}

function AppearanceSection() {
  const { mode, setMode } = useTheme();
  return (
    <>
      <h3>外观</h3>
      <p className="aw-settings-lede">
        「跟随系统」不写 <code>data-theme</code>，由 <code>light-dark()</code> 自己按 <code>prefers-color-scheme</code> 解析；另外两档写死，压过系统的偏好。
      </p>
      <div className="aw-settings-choices" role="radiogroup" aria-label="主题">
        {THEME_MODES.map((option) => {
          const Icon = THEME_LABEL[option].icon;
          return (
            <button
              aria-checked={mode === option}
              className={`aw-settings-choice${mode === option ? " is-on" : ""}`}
              key={option}
              onClick={() => setMode(option)}
              role="radio"
              type="button"
            >
              <Icon aria-hidden="true" size={18} />
              <span>{THEME_LABEL[option].text}</span>
            </button>
          );
        })}
      </div>
    </>
  );
}
