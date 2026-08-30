/**
 * 每一轮那行脚注。
 *
 * 它在三个模式里是同一个零件，所以这里钉的是那三处都不能各自走样的东西：
 *
 * 一是**缺席不画**。用户那条消息、还没落定的那一轮，都没有自己的花销。给它们一个
 * 零会在每一轮下面多出一行说谎的脚注——而 `0` 和「这里问不出答案」在屏幕上必须
 * 长得不一样。这条也是 API 上 `usage` 是 `null` 而不是零值的原因。
 *
 * 二是**不足一美分不写成 $0.00**。这个控制台上一轮通常就值几分之一美分。
 *
 * 三是**缓存显示的是命中率不是命中量**。一个百分比读者当场能用（「这轮基本是
 * 缓存」），一个绝对值还得先和输入相除。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TurnUsageView } from "../api/types";
import { TurnUsage } from "./TurnUsage";

function usage(overrides: Partial<TurnUsageView> = {}): TurnUsageView {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cost_micro_usd: 0,
    ...overrides,
  };
}

describe("问不出答案的时候什么都不画", () => {
  it("null 不渲染", () => {
    const { container } = render(<TurnUsage usage={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("undefined 不渲染——服务端没给和这一轮没花是同一件事：都别画", () => {
    const { container } = render(<TurnUsage usage={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("但真的花了零的一轮要画出来，因为那是个答案", () => {
    const { container } = render(<TurnUsage usage={usage({ input_tokens: 12 })} />);
    expect(container).not.toBeEmptyDOMElement();
    expect(screen.getByText("$0")).toBeInTheDocument();
  });
});

describe("钱", () => {
  it("不足一美分写四位小数，不写 $0.00", () => {
    render(<TurnUsage usage={usage({ cost_micro_usd: 2_100 })} />);
    expect(screen.getByText("$0.0021")).toBeInTheDocument();
  });

  it("超过一美元收回两位", () => {
    render(<TurnUsage usage={usage({ cost_micro_usd: 1_500_000 })} />);
    expect(screen.getByText("$1.50")).toBeInTheDocument();
  });
});

describe("缓存", () => {
  it("有命中时显示命中率", () => {
    render(
      <TurnUsage
        usage={usage({ input_tokens: 10_000, cache_read_tokens: 7_100 })}
      />,
    );
    expect(screen.getByText("缓存 71%")).toBeInTheDocument();
  });

  it("没有命中时整段不出现，而不是写「缓存 0%」", () => {
    render(<TurnUsage usage={usage({ input_tokens: 10_000 })} />);
    expect(screen.queryByText(/缓存/)).toBeNull();
  });

  it("命中数不加进输入——两个数分别显示", () => {
    render(
      <TurnUsage
        usage={usage({
          input_tokens: 10_000,
          output_tokens: 500,
          cache_read_tokens: 8_000,
        })}
      />,
    );
    // 输入仍然是 10.0k，不是 18.0k。
    expect(screen.getByText("10.0k")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
  });
});

describe("用时", () => {
  it("页面知道的时候写出来", () => {
    render(<TurnUsage seconds={9} usage={usage({ input_tokens: 1 })} />);
    expect(screen.getByText("9s")).toBeInTheDocument();
  });

  it("不知道就不写，而不是写 0s", () => {
    render(<TurnUsage usage={usage({ input_tokens: 1 })} />);
    expect(screen.queryByText(/\ds/)).toBeNull();
  });

  it("超过一分钟收成分钟", () => {
    render(<TurnUsage seconds={124} usage={usage({ input_tokens: 1 })} />);
    expect(screen.getByText("2m")).toBeInTheDocument();
  });
});
