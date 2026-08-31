import { useQuery } from "@tanstack/react-query";
import { Code2, ListTodo, MessageSquare, Wallet } from "lucide-react";
import { useState } from "react";

import { getUsage } from "../../api/client";
import type { UsageBucket, UsageResponse, UsageWindow } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { ErrorNotice, LoadingLine } from "../../components/ui";

/**
 * 钱和 token 花在哪了。
 *
 * **为什么需要单独一页。** 三个模式各自都会报自己那一笔——Chat 每轮的脚注、Task
 * 右栏的一块、子代理面板里的每一条——而每一处读的都是眼前这一个运行。「这个月
 * 都花在哪了」跨越这个租户跑过的每一个运行，那几页里没有一页手上有第二个。
 *
 * **它报账，不记账。** 每个数都是从事件日志里加出来的，而费用是运行**当时**按
 * 那一刻的价目表算好写下的。所以这一页永远不会和它引用的那次运行对不上——代价
 * 是它也永远改不了一次算错价的运行。改了价目表的部署只在新的运行上看到新价。这
 * 是 `domain/pricing.py` 早就做过的取舍，在这里重复一遍是因为一个报表页最容易
 * 被当成账单。
 *
 * **不换算成人民币。** 服务端送来的是 micro-USD 整数，这里显示的也是美元。一个
 * 人民币金额需要一个这个进程没有的汇率，编一个出来会让这一页最像账单的那个数字
 * 恰好是唯一一个没有出处的。
 */

const WINDOWS: { id: UsageWindow; label: string }[] = [
  { id: "7d", label: "7 天" },
  { id: "30d", label: "30 天" },
  { id: "all", label: "全部" },
];

/** 合计那一行里的窗口名。服务端的 `30d` 是个参数值，不是给人读的一句话。 */
const WINDOW_TEXT: Record<UsageWindow, string> = {
  "7d": "最近 7 天",
  "30d": "最近 30 天",
  all: "全部时间",
};

const MODES: { id: string; label: string; icon: typeof MessageSquare }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "task", label: "Tasks", icon: ListTodo },
  { id: "code", label: "Code", icon: Code2 },
];

const EMPTY: UsageBucket = {
  tokens: {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  },
  cost_micro_usd: 0,
  runs: 0,
};

/**
 * `1234567` → `1.23M`，`74100` → `74.1k`，`812` → `812`。
 *
 * 千位以下保持原样。这一页上的小数字多半是「跑了一次、几百个 token」，写成
 * `0.8k` 既没有更短也丢掉了唯一的信息。
 */
export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/**
 * micro-USD → 美元，0 是「—」。
 *
 * 四位小数，不是两位：这个控制台上一次会话经常是几分之一美分，而两位小数会把
 * 一整页的真实花销显示成一列 `$0.00`——那比不显示更坏，因为它看起来是个答案。
 * 超过一美元才收回到两位，那时四位小数只是噪声。
 *
 * **零写成破折号，不写成 `$0`。** 在任何一份价目表下，一次真的花掉零微美元的
 * 运行都不存在，所以这一页上的 `$0` 只有一个来源：这台部署没配
 * `[model.*.pricing]`。把它印成一个价格，等于给整页贴一个「免费」的标签——而这
 * 一页底下那条警告存在的意义正是拆穿这个误读，两者当场打架。`TurnUsage` 那边
 * 早一步做了同样的处理（它是整段不画，因为脚注里没有位置放破折号）；这里画
 * 「—」而不是留空，是因为它在一张表里，空单元格读成的是「这一行漏了」。
 */
export function formatCost(microUsd: number): string {
  if (microUsd === 0) return "—";
  const dollars = microUsd / 1_000_000;
  return dollars >= 1 ? `$${dollars.toFixed(2)}` : `$${dollars.toFixed(4)}`;
}

/** 命中率。没有输入就没有比例——不是 0%，是没有这个数。 */
function cacheRate(bucket: UsageBucket): number | null {
  const input = bucket.tokens.input_tokens;
  if (input <= 0) return null;
  return bucket.tokens.cache_read_tokens / input;
}

function totalTokens(bucket: UsageBucket): number {
  // 命中的那部分**不**再加一遍：它是 `input_tokens` 的子集，服务商的口径。
  return (
    bucket.tokens.input_tokens +
    bucket.tokens.output_tokens +
    bucket.tokens.cache_write_tokens
  );
}

function ModeCard({
  bucket,
  icon: Icon,
  label,
}: {
  bucket: UsageBucket;
  icon: typeof MessageSquare;
  label: string;
}) {
  const rate = cacheRate(bucket);
  return (
    <div className="aw-usage-mode">
      <div className="aw-usage-mode-head">
        <span className="aw-usage-mode-icon">
          <Icon aria-hidden="true" size={15} />
        </span>
        <strong>{label}</strong>
        <span className="aw-usage-mode-runs">
          {bucket.runs === 0 ? "没跑过" : `${String(bucket.runs)} 次运行`}
        </span>
      </div>
      <div className="aw-usage-mode-figures">
        <span className="aw-usage-tokens">{formatTokens(totalTokens(bucket))}</span>
        <span className="aw-usage-cost">{formatCost(bucket.cost_micro_usd)}</span>
      </div>
      <dl className="aw-usage-split">
        <dt>输入</dt>
        <dd>{formatTokens(bucket.tokens.input_tokens)}</dd>
        <dt>输出</dt>
        <dd>{formatTokens(bucket.tokens.output_tokens)}</dd>
        <dt>缓存命中</dt>
        <dd>
          {formatTokens(bucket.tokens.cache_read_tokens)}
          {rate === null ? "" : `（${String(Math.round(rate * 100))}%）`}
        </dd>
      </dl>
    </div>
  );
}

/**
 * 这一页的正文，抽出来给设置面板里那一格用。
 *
 * 抽的是**除了 `<h1>` 以外的全部**：一块内容在自己的路由上要有标题，在一个已经
 * 有标题的对话框里再挂一个「用量」就是把同一个词说两遍。窗口选择器留在里面，
 * 它是这块内容的控件而不是这一页的。
 */
export function UsageReport({ heading }: { heading?: React.ReactNode }) {
  const { identity } = useIdentity();
  const [window, setWindow] = useState<UsageWindow>("30d");

  const usage = useQuery({
    queryKey: ["usage", identity, window],
    queryFn: ({ signal }) => getUsage(identity, { window, signal }),
    // 这一页是会被反复刷新的那一种。半分钟内不重打接口，但离开再回来要重取——
    // 一个显示着上周数字、看起来却是刚打开的页面是最难被发现的那种错。
    staleTime: 30_000,
  });

  const report: UsageResponse | undefined = usage.data;
  const byMode = report?.by_mode ?? {};
  const grandTotal = MODES.reduce(
    (sum, mode) => sum + (byMode[mode.id] ?? EMPTY).cost_micro_usd,
    0,
  );
  const grandTokens = MODES.reduce(
    (sum, mode) => sum + totalTokens(byMode[mode.id] ?? EMPTY),
    0,
  );

  return (
    <div className="aw-usage-page">
      <header className="aw-usage-header">
        <div>
          {heading}
          <p>
            按已经结束的运行统计。费用是每次运行当时按价目表算好写下的，这里只做加法。
          </p>
        </div>
        <div className="aw-usage-windows" role="group" aria-label="统计窗口">
          {WINDOWS.map((option) => (
            <button
              aria-pressed={window === option.id}
              className={`aw-usage-window${window === option.id ? " is-active" : ""}`}
              key={option.id}
              onClick={() => {
                setWindow(option.id);
              }}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
      </header>

      {usage.isPending ? <LoadingLine label="正在统计用量" /> : null}
      {usage.isError ? (
        <ErrorNotice message={`读不到用量：${String(usage.error)}`} />
      ) : null}

      {report === undefined ? null : (
        <>
          <section className="aw-usage-total" aria-label="合计">
            <span className="aw-usage-total-icon">
              <Wallet aria-hidden="true" size={18} />
            </span>
            <div>
              <strong>{formatCost(grandTotal)}</strong>
              <span>
                {formatTokens(grandTokens)} tokens ·{" "}
                {WINDOW_TEXT[report.window]}
              </span>
            </div>
            {report.runs_in_flight > 0 ? (
              /* 说明，不是一笔用量。一个还在跑的运行没有写下终止事件，所以它
                 一个 token 都没有计进上面那个数——沉默地少报，比多说一句更坏。 */
              <p className="aw-usage-inflight">
                还有 {report.runs_in_flight} 个运行没结束，它们的用量不在上面这个数里。
              </p>
            ) : null}
          </section>

          <section aria-label="按模式" className="aw-usage-modes">
            {MODES.map((mode) => (
              <ModeCard
                bucket={byMode[mode.id] ?? EMPTY}
                icon={mode.icon}
                key={mode.id}
                label={mode.label}
              />
            ))}
          </section>

          {report.delegated.runs > 0 ? (
            <p className="aw-usage-note">
              Tasks 那一格里有 <strong>{formatTokens(totalTokens(report.delegated))}</strong>{" "}
              是 {report.delegated.runs} 个子代理烧的（{formatCost(report.delegated.cost_micro_usd)}）。它已经算在 Tasks 里了，<strong>不要再加一遍</strong>——子代理有自己的上限，主运行的预算从来看不见这一笔。
            </p>
          ) : null}

          <section aria-label="按模型" className="aw-usage-models">
            <h2>按模型</h2>
            {Object.keys(report.by_model).length === 0 ? (
              <p className="aw-usage-note">这个窗口里没有已经结束的运行。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">模型档</th>
                    <th scope="col">运行</th>
                    <th scope="col">输入</th>
                    <th scope="col">命中</th>
                    <th scope="col">输出</th>
                    <th scope="col">费用</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.by_model).map(([name, bucket]) => (
                    <tr key={name}>
                      <th scope="row">{name}</th>
                      <td>{bucket.runs}</td>
                      <td>{formatTokens(bucket.tokens.input_tokens)}</td>
                      <td>{formatTokens(bucket.tokens.cache_read_tokens)}</td>
                      <td>{formatTokens(bucket.tokens.output_tokens)}</td>
                      <td>{formatCost(bucket.cost_micro_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {report.unpriced_profiles.length > 0 ? (
            /* 「没配价目表」而不是「免费」。零花销和零价目表在屏幕上长得一样，
               而这两件事要做的处理完全不同——后者要去改一份配置。 */
            <p className="aw-usage-note is-warning">
              <strong>{report.unpriced_profiles.join("、")}</strong>{" "}
              这个窗口里记下的费用是 0。多半是这台部署没给它配价目表（
              <code>[model.&lt;档名&gt;.pricing]</code>），不是这个模型不要钱——这个仓库不出厂任何价格，因为它不知道你的合同。
            </p>
          ) : null}

          <p className="aw-usage-note">
            缓存命中单列一行，不折进输入：两段的单价不一样，合并之后任何一个乘法都是错的。
          </p>
        </>
      )}
    </div>
  );
}

/** 路由上的那一页：正文加一个 `<h1>`。 */
export function UsagePage() {
  return <UsageReport heading={<h1>用量</h1>} />;
}
