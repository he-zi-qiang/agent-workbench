import { describe, expect, it } from "vitest";
import { explainFailure, explainRunFailure } from "./failure";

describe("explainFailure", () => {
  it("turns the server's sentence into one a reader can act on", () => {
    // The exact string the Worker writes when a provider call fails.
    const failure = explainFailure(
      "the understand step failed with provider_error (retryable) during start",
    );

    expect(failure).not.toBeNull();
    expect(failure?.text).toContain("理解目标");
    expect(failure?.text).toContain("调用模型服务失败");
    expect(failure?.retryable).toBe(true);
    // The class name that started all this must not survive into the copy.
    expect(failure?.text).not.toContain("AgentNodeFailedError");
  });

  it("does not offer a retry for a cause that would fail the same way", () => {
    const failure = explainFailure(
      "the export step failed with policy_denied (not retryable) during resume_with_approval",
    );

    expect(failure?.retryable).toBe(false);
    expect(failure?.text).toContain("被权限策略拒绝");
  });

  it("sends a refused account to the provider, not back to the config", () => {
    // ADR-0084. The default sentence for anything non-retryable is 「需要先改动
    // 任务或配置」, and for this one both halves of that are wrong: the task is
    // fine and so is the config. Sending a reader to re-read their own YAML
    // when their account has no credit left is exactly the misdirection this
    // code was split out of `provider_error` to end.
    const failure = explainFailure(
      "the work step failed with provider_account_rejected (not retryable) during start",
    );

    expect(failure?.retryable).toBe(false);
    expect(failure?.text).toContain("模型服务拒绝了这个部署的账号");
    expect(failure?.text).toContain("充值");
    expect(failure?.text).not.toContain("改动任务或配置");
  });

  it("explains a step that produced nothing", () => {
    const failure = explainFailure(
      "the synthesize step did not produce usable output during start",
    );

    expect(failure?.text).toContain("撰写草稿");
    expect(failure?.retryable).toBe(false);
  });

  it("shows an unrecognised detail rather than replacing it", () => {
    // A detail this table has not learned is still the most specific thing
    // anyone has; swallowing it would leave the reader with strictly less.
    const raw = "the graph raised SomethingNewError during start";

    expect(explainFailure(raw)).toEqual({ text: raw, retryable: false });
  });

  it("has nothing to say about a Task that did not fail", () => {
    expect(explainFailure(null)).toBeNull();
    expect(explainFailure("   ")).toBeNull();
  });
});

describe("一个运行为什么停下来", () => {
  it("输出上限说清楚是「一次话太长」，而不是「总额用完了」", () => {
    // 实测那一行读起来是自相矛盾的：`token 预算用尽` 配着 `17.2k/30.0k`——
    // 一个没满的分数。矛盾是假的：撞的是 max_output_tokens，另一个上限，
    // 而且是这个面板根本画不出来的那个（RunStarted.budget 不带它）。
    expect(
      explainRunFailure("the model stopped at its output token ceiling"),
    ).toContain("单次回答");
  });

  it("运行自己的天花板说得出是哪一道", () => {
    expect(explainRunFailure("the run passed its ceiling: token_budget")).toBe(
      "这次运行用完了它自己的 token 预算。",
    );
    expect(explainRunFailure("the run passed its ceiling: max_steps")).toBe(
      "这次运行用完了它自己的步数。",
    );
  });

  it("模型调用超时把秒数带出来——那个数就是要改的那个", () => {
    expect(
      explainRunFailure("the model call exceeded the runtime's 120.0s envelope"),
    ).toBe("一次模型调用超过了运行时给单次调用的 120.0 秒上限。");
  });

  it("子代理没跑完时说得出是哪一个、为什么", () => {
    expect(explainRunFailure("sub-agent analyst ended as failed (token_budget)")).toBe(
      "子代理 analyst 没跑完（token 预算）。",
    );
  });

  it("没学过的句子返回 null，让调用方原样显示服务端的原话", () => {
    // 与 explainFailure 同一条规则：还没被认出来的句子，仍然是最具体的那份
    // 信息，换成「未知原因」是把它扔掉。
    expect(explainRunFailure("something nobody has written a phrase for")).toBeNull();
  });

  it("认不出来的天花板名字原样带出来，而不是吞掉", () => {
    expect(explainRunFailure("the run passed its ceiling: some_new_reason")).toBe(
      "这次运行用完了它自己的 some_new_reason。",
    );
  });
});
