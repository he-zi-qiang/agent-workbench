/**
 * 用量页说的话。
 *
 * 后端那两份测试钉的是数对不对（`tests/adapters/test_usage_report.py` 管算术，
 * `tests/api/test_usage_api.py` 管连接和租户隔离）。这一份钉的是**同一批数被读成
 * 什么**，而那是三件很容易做反的事：
 *
 * 一是 `$0.00`。这个控制台上一次会话经常只值几分之一美分，两位小数会把一整页真
 * 实花销显示成一列零——那比不显示更坏，因为它看起来是个答案。
 * 二是缓存命中被折进输入，于是页面报出一个比实际发出去的还大的提示词。
 * 三是子代理那一笔被加了两遍，或者被从 Tasks 里减掉。两种都错，而且方向相反。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { UsageBucket, UsageResponse } from "../../api/types";
import { UsagePage, formatCost, formatTokens } from "./UsagePage";

const getUsage = vi.hoisted(() => vi.fn());

vi.mock("../../api/client", () => ({ getUsage }));
vi.mock("../../app/IdentityContext", () => ({
  useIdentity: () => ({
    identity: { tenantId: "t", principalId: "p", scopes: [] },
  }),
}));

function bucket(overrides: Partial<UsageBucket["tokens"]> = {}, extra = {}): UsageBucket {
  return {
    tokens: {
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      ...overrides,
    },
    cost_micro_usd: 0,
    runs: 0,
    ...extra,
  };
}

function report(overrides: Partial<UsageResponse> = {}): UsageResponse {
  return {
    window: "30d",
    since: "2026-07-30T00:00:00Z",
    until: "2026-08-29T00:00:00Z",
    by_mode: { chat: bucket(), code: bucket(), task: bucket() },
    by_model: {},
    delegated: bucket(),
    runs_in_flight: 0,
    unpriced_profiles: [],
    ...overrides,
  };
}

function draw(response: UsageResponse) {
  getUsage.mockResolvedValue(response);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <UsagePage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  getUsage.mockReset();
});

describe("钱要写得看得见", () => {
  it("不足一美分的花销不显示成 $0.00", () => {
    // 2100 micro-USD = $0.0021。两位小数会把它变成 $0.00，而这一页上大部分
    // 真实数字都长这样。
    expect(formatCost(2_100)).toBe("$0.0021");
  });

  it("超过一美元收回两位小数——那时四位只是噪声", () => {
    expect(formatCost(12_820_000)).toBe("$12.82");
  });

  it("零写成破折号，不写成一个价格", () => {
    // 一次真的花掉零微美元的运行不存在，所以这一页上的零只有一个来源：这台部署
    // 没配价目表。印成 `$0` 等于给整页贴一个「免费」的标签，而这一页底下那条
    // 警告存在的意义正是拆穿它。
    expect(formatCost(0)).toBe("—");
  });
});

describe("token 的量级", () => {
  it("千位以下原样，因为那时那几位就是全部信息", () => {
    expect(formatTokens(812)).toBe("812");
  });

  it("千位以上收成 k，百万以上收成 M", () => {
    expect(formatTokens(74_100)).toBe("74.1k");
    expect(formatTokens(1_840_000)).toBe("1.84M");
  });
});

describe("三个模式", () => {
  it("没花过的模式显示为「没跑过」，而不是从页面上消失", async () => {
    draw(report());

    expect(await screen.findByText("Chat")).toBeInTheDocument();
    expect(screen.getByText("Tasks")).toBeInTheDocument();
    expect(screen.getByText("Code")).toBeInTheDocument();
    expect(screen.getAllByText("没跑过")).toHaveLength(3);
  });

  it("缓存命中单独一行，不加进输入", async () => {
    draw(
      report({
        by_mode: {
          chat: {
            tokens: {
              input_tokens: 1_000_000,
              output_tokens: 100_000,
              cache_read_tokens: 800_000,
              cache_write_tokens: 0,
            },
            cost_micro_usd: 5_000_000,
            runs: 42,
          },
          code: bucket(),
          task: bucket(),
        },
      }),
    );

    // 合计是 输入 + 输出 + 缓存写入 = 1.10M。把命中的 800k 也加进去会得到
    // 1.90M——一个比实际发出去的还大的提示词。
    expect(await screen.findByText("1.10M")).toBeInTheDocument();
    expect(screen.getByText(/800\.0k（80%）/)).toBeInTheDocument();
  });
});

describe("子代理那一笔", () => {
  it("说清它已经算在 Tasks 里了，不要再加一遍", async () => {
    draw(
      report({
        delegated: {
          tokens: {
            input_tokens: 400_000,
            output_tokens: 40_000,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
          },
          cost_micro_usd: 360_000,
          runs: 3,
        },
      }),
    );

    const note = await screen.findByText(/子代理烧的/);
    expect(note.textContent).toContain("不要再加一遍");
  });

  it("没有子代理时整句不出现", async () => {
    draw(report());
    await screen.findByText("Chat");
    expect(screen.queryByText(/子代理烧的/)).toBeNull();
  });
});

describe("零费用的两种原因要分开说", () => {
  it("没配价目表的档被点名，并指到该改的那一节配置", async () => {
    draw(report({ unpriced_profiles: ["main", "compact"] }));

    const warning = await screen.findByText(/没给它配价目表/);
    expect(warning.textContent).toContain("main、compact");
    expect(warning.textContent).toContain("不是这个模型不要钱");
  });

  it("都配了价的时候不出现这句", async () => {
    draw(report());
    await screen.findByText("Chat");
    expect(screen.queryByText(/没给它配价目表/)).toBeNull();
  });
});

describe("还在跑的运行是说明，不是用量", () => {
  it("有未结束的运行时说出来", async () => {
    draw(report({ runs_in_flight: 2 }));
    expect(
      await screen.findByText(/还有 2 个运行没结束/),
    ).toBeInTheDocument();
  });

  it("没有时不说", async () => {
    draw(report());
    await screen.findByText("Chat");
    expect(screen.queryByText(/没结束/)).toBeNull();
  });
});

describe("按模型的表", () => {
  it("列出每一档，命中和输入分成两列", async () => {
    draw(
      report({
        by_model: {
          main: {
            tokens: {
              input_tokens: 2_100_000,
              output_tokens: 318_000,
              cache_read_tokens: 4_420_000,
              cache_write_tokens: 0,
            },
            cost_micro_usd: 6_310_000,
            runs: 1_284,
          },
        },
      }),
    );

    const row = await screen.findByRole("row", { name: /main/ });
    expect(within(row).getByText("1284")).toBeInTheDocument();
    expect(within(row).getByText("2.10M")).toBeInTheDocument();
    expect(within(row).getByText("4.42M")).toBeInTheDocument();
    expect(within(row).getByText("$6.31")).toBeInTheDocument();
  });

  it("窗口里没有已结束的运行时说出来，而不是画一张空表", async () => {
    draw(report());
    expect(
      await screen.findByText("这个窗口里没有已经结束的运行。"),
    ).toBeInTheDocument();
  });
});

describe("窗口", () => {
  it("切换窗口会用新窗口重新去问", async () => {
    draw(report());
    await screen.findByText("Chat");

    await userEvent.click(screen.getByRole("button", { name: "7 天" }));

    expect(getUsage).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ window: "7d" }),
    );
  });
});
