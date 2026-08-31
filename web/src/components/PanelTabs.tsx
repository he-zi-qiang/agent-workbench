import { useId, useRef } from "react";

/**
 * 右栏顶上那一排标签页。
 *
 * **为什么是标签页，不是一列往下堆的分节。** 右栏只有一栏宽、一屏高，而想住进
 * 来的东西比这多：Code 那边是目录、预览、工作区；Task 那边是进度、子代理、产物、
 * 上下文、事件。往下堆的代价不是「要滚动」——是**第五节永远没人看见**，因为读者
 * 不知道它在那儿。标签页把「有哪些」和「现在看哪个」拆成两件事，前者一眼看完。
 *
 * **为什么长得像浏览器的标签，不是分段控件（segmented control）。** 上一版是后
 * 者。分段控件说的是「同一个东西的几种视图」（日/周/月），而这里每一格装的是**不
 * 同的东西**——目录不是预览的另一种画法。浏览器标签正是为后者发明的形状：底边和
 * 内容连成一片，选中的那一枚盖住分隔线，于是「这一格是那一片内容的把手」这件事
 * 由形状本身说出来，不用文字解释。
 *
 * **空的那一格不画。** 一个任务还没派过子代理时，「子代理」这一枚不出现，而不是
 * 出现一个点进去空空如也的格子。这条和这个仓库里其他地方是同一条：画得出来但点
 * 了什么也没有，读者读成的是「坏了」，不是「不适用」。代价是标签的数量会变——所
 * 以选中项在这里是**算出来的**（见 `current`），不是存下来的：存下来的那个 id 一
 * 旦对应的格子消失，整栏就空白了。
 */

export interface PanelTabEntry {
  id: string;
  label: string;
  /**
   * 标签上那个小数字。
   *
   * 只给「有几个」有意义的格子（子代理 3、产物 5）。给「进度」配一个数字要先回
   * 答「几之几」，而那正是这一栏另一处刻意不回答的问题。
   */
  count?: number | undefined;
  /**
   * 这一格现在有没有东西。`false` 的格子整枚不画。
   *
   * 默认 `true`：绝大多数格子是常驻的，让常驻的那些不必写这个字段。
   */
  available?: boolean | undefined;
  body: React.ReactNode;
}

export function PanelTabs({
  active,
  entries,
  label,
  onSelect,
  trailing,
}: {
  /** 想选中的那一格。它不在（或已消失）时落回第一格。 */
  active: string | null;
  entries: readonly PanelTabEntry[];
  /** 给这一排标签的无障碍名字，比如「这个任务的几栏」。 */
  label: string;
  onSelect: (id: string) => void;
  /** 标签条右端那块地方，收起按钮之类。 */
  trailing?: React.ReactNode;
}) {
  const base = useId();
  const strip = useRef<HTMLDivElement>(null);
  const shown = entries.filter((entry) => entry.available !== false);

  // 选中项算出来，不存下来：格子会随任务的进展出现和消失（第一次派出子代理时多
  // 一枚，产物写出来时又多一枚），而一个存着的 id 在它那一枚消失的瞬间会让整栏
  // 变成空白。这里退回第一枚，是「消失了就看下一个能看的」。
  const current = shown.find((entry) => entry.id === active) ?? shown[0];
  if (current === undefined) return null;

  // 左右方向键在标签之间走，这是 tablist 的标准键盘契约；没有它，一排 `role=tab`
  // 对读屏用户是一排说了谎的按钮。
  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    // 只认从标签上发出来的按键。`trailing` 渲染在这条 strip **里面**（预览栏把
    // 「收起」按钮放在那儿），而这个 handler 挂在外层，所以在那颗按钮上按左右键
    // 会冒泡到这里：preventDefault 之后换掉选中项，再把焦点从按钮上抢到标签上。
    // 读者想做的是在控件之间移动，得到的是右栏内容在脚下换了一张、焦点也不见了。
    if (!(event.target as HTMLElement).closest('[role="tab"]')) return;
    const step =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const at = shown.findIndex((entry) => entry.id === current?.id);
    const next = shown[(at + step + shown.length) % shown.length];
    if (next === undefined) return;
    onSelect(next.id);
    // 焦点跟着走。漫游 tabindex（roving tabindex）下只有选中那一枚可聚焦，
    // 所以换选中之后必须把焦点也搬过去，否则下一次按键落在一个已经 -1 的元素上。
    requestAnimationFrame(() => {
      strip.current
        ?.querySelector<HTMLButtonElement>(`#${CSS.escape(`${base}-${next.id}`)}`)
        ?.focus();
    });
  }

  return (
    <>
      <div
        aria-label={label}
        className="aw-panel-tabs"
        onKeyDown={onKeyDown}
        ref={strip}
        role="tablist"
      >
        {shown.map((entry) => {
          const on = entry.id === current.id;
          return (
            <button
              aria-controls={`${base}-${entry.id}-body`}
              aria-selected={on}
              className={`aw-panel-tab${on ? " is-on" : ""}`}
              id={`${base}-${entry.id}`}
              key={entry.id}
              onClick={() => onSelect(entry.id)}
              role="tab"
              tabIndex={on ? 0 : -1}
              type="button"
            >
              <span>{entry.label}</span>
              {entry.count === undefined ? null : (
                <span className="aw-panel-tab-count">{entry.count}</span>
              )}
            </button>
          );
        })}
        {trailing === undefined ? null : (
          <div className="aw-panel-tabs-trailing">{trailing}</div>
        )}
      </div>
      <div
        aria-labelledby={`${base}-${current.id}`}
        className="aw-panel-tab-body"
        id={`${base}-${current.id}-body`}
        role="tabpanel"
        // 内容区自己可聚焦，否则用 Tab 从标签走进内容时，一个只有文字没有控件的
        // 格子（比如「上下文」）会被整个跳过——读屏用户听不到它存在。
        tabIndex={0}
      >
        {current.body}
      </div>
    </>
  );
}
