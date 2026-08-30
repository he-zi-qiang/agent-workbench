import { Zap } from "lucide-react";

import type { TurnUsageView } from "../api/types";

/**
 * 一轮花了多少，贴在那一轮下面。
 *
 * **为什么是脚注而不是一块面。** 这个数在三个模式里都存在，而读者只在两种时刻
 * 要它：一轮特别慢或特别贵的时候，和月底对账的时候。后者是用量页；前者要的是
 * 「就在这里，不用点开」。任何需要展开的形式都会让第一种时刻问不出这个问题——
 * 而那正是唯一会当场问的时刻。
 *
 * **缺席不画。** 用户那一条没有花销，还没落定的那一轮也没有。给它们一个零会让
 * 每一轮下面多出一行说谎的脚注，而 `0` 和「这里问不出答案」在屏幕上必须长得不
 * 一样——这也是 `usage` 在 API 上是 `null` 而不是零值的原因。
 *
 * **缓存命中单列，不折进输入。** 它是 `input_tokens` 的子集（服务商的口径），
 * 加进去会把每一次命中的提示词算两遍。这里显示的是命中**率**而不是命中量：一
 * 个百分比是读者当场能用的（「这一轮基本是缓存」），而一个绝对值还要先和输入
 * 相除。
 *
 * **钱按美元，不换算。** 换算需要一个这个进程没有的汇率。小额四位小数，理由和
 * 用量页那一处一样：两位会把这一页真实花销全显示成 `$0.00`。
 */

/** micro-USD → 美元。和 `UsagePage` 同形，刻意不共用——见文件末尾。 */
function money(microUsd: number): string {
  if (microUsd === 0) return "$0";
  const dollars = microUsd / 1_000_000;
  return dollars >= 1 ? `$${dollars.toFixed(2)}` : `$${dollars.toFixed(4)}`;
}

function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/**
 * 和 `TurnUsageView` 一样，只是钱可以是 `null`。
 *
 * Task 的一「步」需要这个放宽：费用是运行**当时**按价目表算好、随终止事件一次
 * 写下的，粒度是运行不是步骤。一步能加出 token（把它那几次 `ModelCompleted`
 * 的用量相加），但加不出钱——要在读的时候重新定价才行，而那正是这套东西一直
 * 拒绝做的事。所以那里显示 token、不显示钱，而不是显示一个 `$0`。
 */
export type PartialTurnUsage = Omit<TurnUsageView, "cost_micro_usd"> & {
  cost_micro_usd: number | null;
};

export function TurnUsage({
  usage,
  seconds,
  label,
}: {
  usage: PartialTurnUsage | null | undefined;
  /** 这一轮跑了多久，页面知道的时候给。不知道就不写，不写零。 */
  seconds?: number | undefined;
  /**
   * 前缀，给这一行换个主语。
   *
   * 同一个零件既贴在一轮下面，也贴在输入框上方当整段会话的合计——两处的数字长得
   * 一样，不写主语的话，页脚那一行会被读成「最后一轮花了这么多」。
   */
  label?: string | undefined;
}) {
  if (usage === null || usage === undefined) return null;

  const rate =
    usage.input_tokens > 0
      ? Math.round((usage.cache_read_tokens / usage.input_tokens) * 100)
      : null;

  return (
    <p className={`aw-turn-usage${label === undefined ? "" : " is-total"}`}>
      <Zap aria-hidden="true" size={12} />
      {label === undefined ? null : <span className="aw-turn-usage-label">{label}</span>}
      <span>{tokens(usage.input_tokens)}</span>
      <span aria-label="到" className="aw-turn-usage-arrow">
        →
      </span>
      <span>{tokens(usage.output_tokens)}</span>
      {rate !== null && rate > 0 ? (
        <>
          <span className="aw-turn-usage-dot">·</span>
          <span>缓存 {rate}%</span>
        </>
      ) : null}
      {usage.cost_micro_usd === null ? null : (
        <>
          <span className="aw-turn-usage-dot">·</span>
          <strong>{money(usage.cost_micro_usd)}</strong>
        </>
      )}
      {seconds === undefined ? null : (
        <>
          <span className="aw-turn-usage-dot">·</span>
          <span>{seconds < 60 ? `${String(seconds)}s` : `${String(Math.round(seconds / 60))}m`}</span>
        </>
      )}
    </p>
  );
}

/*
 * `money` 和 `tokens` 与 `UsagePage` 里那两个同形而不共用，和 `DelegationScope`
 * 与 `RunPanel` 之间那对 `formatTokens` 是同一种刻意重复。
 *
 * 那边的注释写得很清楚：两处今天恰好同形，但它们不为同一件事负责。用量页格式化
 * 的是一个月的合计——那里 `$12.82` 是常态，四位小数是噪声；这里格式化的是一轮
 * 的花销，`$0.0021` 是常态。两者今天用同一条规则得出同一个结果，是因为那条规则
 * 恰好同时适合两种量级，而不是因为它们必须一致。合并会让下一个想给月度合计加上
 * 千分位分隔符的人，顺手改掉每一轮的脚注。
 */

/**
 * 把若干轮加起来，得到一段会话的合计。
 *
 * 只加**答得出**的那些：`null` 的轮次（还在跑、或者早于这个字段的历史）不参与，
 * 也不因此把整个合计变成 `null`——一段会话里有一轮答不出，不该让另外九轮的账也
 * 消失。钱只在每一轮都报得出钱时才加：混着算会得到一个「一部分轮次的费用」，
 * 而它看起来和总额一模一样。
 */
export function sumTurnUsage(
  parts: readonly (PartialTurnUsage | null | undefined)[],
): PartialTurnUsage | null {
  const known = parts.filter(
    (part): part is PartialTurnUsage => part !== null && part !== undefined,
  );
  if (known.length === 0) return null;
  const everyPriced = known.every((part) => part.cost_micro_usd !== null);
  return {
    input_tokens: known.reduce((n, part) => n + part.input_tokens, 0),
    output_tokens: known.reduce((n, part) => n + part.output_tokens, 0),
    cache_read_tokens: known.reduce((n, part) => n + part.cache_read_tokens, 0),
    cache_write_tokens: known.reduce((n, part) => n + part.cache_write_tokens, 0),
    cost_micro_usd: everyPriced
      ? known.reduce((n, part) => n + (part.cost_micro_usd ?? 0), 0)
      : null,
  };
}
