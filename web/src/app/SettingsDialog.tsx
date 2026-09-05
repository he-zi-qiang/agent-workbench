import {
  BadgeCheck,
  KeyRound,
  Palette,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { PrincipalIdentity } from "../api/types";
import { IconButton } from "../components/ui";
import { ProviderKeyPanel } from "../features/system/ProviderKeyPanel";
import { useIdentity } from "./IdentityContext";
import { THEME_LABEL, THEME_MODES, useTheme } from "./ThemeContext";

/**
 * 设置。一个左边分类、右边内容的对话框。
 *
 * **它替掉的是「本地身份模拟器」。** 那个框只做一件事——改三个 header——而它占着
 * 这个界面上唯一一个「设置」形状的位置：左下角那颗头像。于是这台部署所有其他可
 * 调的东西各自找了个别的地方住。
 *
 * **只装能改的东西，三类。** 第一版装了五类：身份、密钥、外观，再加用量和运行
 * 状态。后两类各自本来就是一整页（`/usage`、`/system`），塞进这个框等于同一份
 * 报表画了两遍，而且是在一个为表单定尺寸的框里画一张六列的表——用户的原话是
 * 「更多和设置有些重复，设置的每个子页面设计也不统一，大小之类的」。两句都对，
 * 而且是同一个原因：报表不是设置。现在这个框只回答「这台东西有什么可调的」，
 * 三类全部有后端或本地状态撑着：身份（三个 header）、密钥（ADR-101）、外观
 * （`ThemeContext`）。用量和运行状态回到「更多」里，那是它们作为**页面**的入口。
 *
 * **分类是竖排的，不是标签页。** 抄的是 Claude Desktop 设置窗的**结构**（左类右容、
 * 每一类一屏），不是它的条目——它那些条目背后的能力这台部署一多半没有。
 *
 * **三类同一个骨架。** 每一类都是：一个 `<h3>`、一段 `.aw-settings-lede` 说这一
 * 类管什么、然后是它的字段或选项、动作贴在字段下面。此前身份那一类的标题叫
 * 「本地身份模拟器」，左栏却写着「本地身份」——同一样东西两个名字，一眼就读成
 * 两样东西。框的高度也固定下来：换一类不该让整个框跳一下。
 *
 * **打开时落在「模型密钥」，不再是「本地身份」。** 上一版落在身份，理由是左下角
 * 那颗头像此前打开的就是身份编辑框。2026-09-04 的评审把这件事列进「产品操作与
 * 工程原理处于同一阅读层」：一个第一次打开设置的人看到的是 Tenant / Principal /
 * Scopes 三个请求头字段，而他要找的多半是「这台部署用哪把 key」。身份模拟仍然
 * 在，左栏里标成「开发与权限演示」——它是给演示对象级授权用的，不是设置里最常
 * 用的那一类。
 */

interface Section {
  id: string;
  label: string;
  icon: LucideIcon;
  hint: string;
}

const SECTIONS: readonly Section[] = [
  { id: "provider", label: "模型密钥", icon: KeyRound, hint: "这台部署用哪把 key 调模型" },
  { id: "appearance", label: "外观", icon: Palette, hint: "浅色、深色，还是跟着系统" },
  { id: "identity", label: "本地身份", icon: BadgeCheck, hint: "开发与权限演示：模拟请求头里的身份" },
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
  const [section, setSection] = useState("provider");
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
    // 焦点关在框里。选择器里保留 `a` 和 `[tabindex]`：密钥那一类里有链接形状的
    // 东西，漏掉的那些会让 Tab 走到框外面去，而背景是 `inert` 的，于是焦点消失
    // 在一个看不见的地方。
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
      {/* 和左栏同一个名字。「模拟器」这个词留给下面那句说明——它说的是**这些
          字段是什么性质**，不是这一类叫什么。 */}
      <h3>本地身份</h3>
      <p className="aw-settings-lede">
        开发与权限演示用。这里模拟的是请求头里的身份，只用于 loopback 开发环境，不是生产登录。切换身份后，服务端会重新执行对象级授权，本地列表也会按身份隔离（ADR-044）。
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
          负责，而它只能保存这一类里的东西——另外两类根本没有待保存的草稿（主题即点
          即生效，密钥有自己的保存）。 */}
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
      {/* 说给用的人听，不说给写 CSS 的人听。上一版这句话是「不写 data-theme，
          由 light-dark() 自己按 prefers-color-scheme 解析」——三个代码里的名字
          压在一个只想选浅色还是深色的人面前。那三个名字仍然是事实（见
          `ThemeContext`），但它们是注释，不是界面。 */}
      <p className="aw-settings-lede">
        「跟随系统」随操作系统当前的浅色或深色走，系统换了它就换；另外两档固定，不随系统变。改了立刻生效，不用保存。
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
