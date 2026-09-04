import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { EventEnvelope, EventPayload } from "../api/types";
import { StepDisclosure } from "./StepDisclosure";
import { describeEvent } from "./stepDetail";

function event(eventType: string, payload: Omit<EventPayload, "kind">): EventEnvelope {
  return {
    schema_version: 1,
    event_id: "evt_1",
    stream_id: "stream_1",
    run_id: "run_1",
    event_type: eventType,
    durability: "durable",
    timestamp: "2026-08-12T10:00:00Z",
    payload: { kind: eventType, ...payload },
    sequence: 1,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
}

function facts(envelope: EventEnvelope): Map<string, string> {
  return new Map(describeEvent(envelope).facts.map((item) => [item.label, item.value]));
}

/** Every value on screen for this event, so a test can assert one is absent. */
function shownValues(envelope: EventEnvelope): string {
  const detail = describeEvent(envelope);
  return [
    detail.summary ?? "",
    ...detail.facts.map((item) => `${item.label}=${item.value}`),
    ...detail.bodies.map((body) => body.text),
  ].join("\n");
}

function runStarted(budget: Record<string, unknown>): EventEnvelope {
  return event("RunStarted", {
    run_kind: "task",
    model_profile: "balanced",
    tool_names: ["knowledge_search"],
    budget: {
      max_steps: 12,
      max_tool_calls: 32,
      max_total_tokens: null,
      max_cost_micro_usd: null,
      deadline: null,
      ...budget,
    },
  });
}

function runEnded(eventType: string, tokens: Record<string, number>): EventEnvelope {
  const payload: Omit<EventPayload, "kind"> = {
    stop_reason: eventType === "RunFailed" ? "error" : "completed",
    usage: {
      steps: 3,
      tool_calls: 1,
      cost_micro_usd: 0,
      tokens: {
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        ...tokens,
      },
    },
  };
  if (eventType === "RunFailed") {
    payload.error = { code: "provider_unavailable", message: "上游没有响应", retryable: true };
  }
  return event(eventType, payload);
}

describe("describeEvent：RunStarted 的预算", () => {
  it("把 token 上限做成事实行", () => {
    expect(facts(runStarted({ max_total_tokens: 16000 })).get("token 上限")).toBe("16000");
  });

  it("没有 token 上限时说出来，而不是留一个破折号", () => {
    // 破折号是"不知道"，这里是"确实没设"——领域层在字段为空时整条检查都跳过。
    expect(facts(runStarted({ max_total_tokens: null })).get("token 上限")).toBe("未设上限");
  });

  it("deadline 显示成能读的时刻，不是 ISO 串也不是 epoch 数字", () => {
    const iso = "2026-08-12T10:30:00Z";
    const shown = facts(runStarted({ deadline: iso })).get("截止时间");
    expect(shown).toBeDefined();
    expect(shown).not.toBe(iso);
    expect(shown).not.toBe(String(Date.parse(iso)));
    expect(shown).toMatch(/^\d{1,2}[/-]\d{1,2}\s\d{2}:\d{2}$/);
  });

  it("没有 deadline 的运行不多出一行空的截止时间", () => {
    expect(facts(runStarted({ deadline: null })).has("截止时间")).toBe(false);
  });

  it("不显示成本上限——没有任何配置设过它", () => {
    // 红线：config/ 六个 TOML 都没设过这个上限，用量侧也没有费率表。
    // 就算事件里带了值，也不该在这里长出一行成本。
    const shown = shownValues(runStarted({ max_cost_micro_usd: 5000 }));
    expect(shown).not.toContain("5000");
    expect(shown).not.toMatch(/成本|费用|cost/i);
  });
});

describe("describeEvent：结束事件的 token 用量", () => {
  it("总计 token 按预算的算法算：缓存命中不重复计，缓存写入要计", () => {
    const shown = facts(
      runEnded("RunCompleted", {
        input_tokens: 100,
        output_tokens: 20,
        cache_read_tokens: 80,
        cache_write_tokens: 30,
      }),
    );
    // (100-80) + 80 + 20 + 30
    expect(shown.get("总计 token")).toBe("150");
    // 把缓存命中当成额外一股 token 加进来会得到 230。
    expect(shown.get("总计 token")).not.toBe("230");
    // 只把屏幕上的输入与输出相加会得到 120——那正是这一行存在的理由。
    expect(shown.get("总计 token")).not.toBe("120");
  });

  it("缓存命中标成输入的一部分，缓存写入单列", () => {
    const shown = facts(
      runEnded("RunCompleted", {
        input_tokens: 100,
        cache_read_tokens: 80,
        cache_write_tokens: 30,
      }),
    );
    expect(shown.get("其中缓存命中")).toBe("80");
    expect(shown.get("缓存写入")).toBe("30");
  });

  it("没有缓存流量时不多出这两行", () => {
    const shown = facts(runEnded("RunCompleted", { input_tokens: 100, output_tokens: 20 }));
    expect(shown.has("其中缓存命中")).toBe(false);
    expect(shown.has("缓存写入")).toBe(false);
    expect(shown.get("总计 token")).toBe("120");
  });

  it("失败的运行也报出它花掉的 token", () => {
    const shown = facts(runEnded("RunFailed", { input_tokens: 40, output_tokens: 5 }));
    expect(shown.get("总计 token")).toBe("45");
    expect(shown.get("错误码")).toBe("provider_unavailable");
  });

  it("不显示用量里的成本——它是从没配过的费率表算出来的", () => {
    const envelope = runEnded("RunCompleted", { input_tokens: 100 });
    const usage = envelope.payload.usage as Record<string, unknown>;
    usage.cost_micro_usd = 4321;
    const shown = shownValues(envelope);
    expect(shown).not.toContain("4321");
    expect(shown).not.toMatch(/成本|费用|cost/i);
  });
});

describe("describeEvent：ModelCompleted 的思考摘要", () => {
  function completed(payload: Record<string, unknown>): EventEnvelope {
    return event("ModelCompleted", {
      model_call_id: "mc_1",
      finish_reason: "stop",
      usage: { input_tokens: 10, output_tokens: 5 },
      ...payload,
    });
  }

  it("把摘录做成正文块，排在提示词与答案之间", () => {
    const detail = describeEvent(
      completed({ text: "答案", thinking_preview: "先看资料再回答。" }),
    );
    const labels = detail.bodies.map((body) => body.label);

    expect(labels).toEqual(["思考摘要", "模型输出"]);
    expect(detail.bodies[0]?.text).toBe("先看资料再回答。");
  });

  it("没思考的调用不多出一个块", () => {
    // `text()` 对缺失字段返回的是「—」而不是空串，所以判空写错方向时，
    // 每一条 ModelCompleted 都会多出一个内容为「—」的块——三个界面同时中招。
    const detail = describeEvent(completed({ text: "答案" }));

    expect(detail.bodies.map((body) => body.label)).toEqual(["模型输出"]);
  });

  it("被围栏抹空的候选也不多出一个块", () => {
    // 检索型 Chat 的形态：文本与摘录都被服务端置空，事件仍然到达。
    const detail = describeEvent(completed({ text: "", thinking_preview: "" }));

    expect(detail.bodies).toHaveLength(0);
  });
});

describe("StepDisclosure 里的提示词", () => {
  it("默认收着，摘要上写着有多长；展开才是整段", () => {
    const prompt = "[system]\n你是一个编码代理。\n\n[user]\n把这段改成两句话。";
    const { container } = render(
      <StepDisclosure
        event={event("ModelStarted", {
          model_call_id: "mc_1",
          model_id: "deepseek-v4-flash",
          prompt_preview: prompt,
        })}
        title="模型调用已开始"
      />,
    );

    // 一个 delegate_agent 的组展开后第一屏全是系统提示词，参数要滚两千字才到。
    const fold = container.querySelector("details.aw-step-prompt") as HTMLDetailsElement;
    expect(fold.open).toBe(false);
    expect(fold.querySelector("summary")?.textContent).toContain(
      `发给模型的提示词 · ${String(prompt.length)} 字`,
    );
    expect(fold.querySelector("pre")?.textContent).toBe(prompt);
  });
});

describe("StepDisclosure", () => {
  it("预算落在事实区，不再只躺在原始事件 JSON 里", () => {
    const { container } = render(
      <StepDisclosure event={runStarted({ max_total_tokens: 16000 })} title="运行已开始" />,
    );
    const factsRegion = container.querySelector(".aw-step-facts");
    expect(factsRegion?.textContent).toContain("token 上限");
    expect(factsRegion?.textContent).toContain("16000");
  });
});
