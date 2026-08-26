import { describe, expect, it } from "vitest";
import { explainFailure } from "./failure";

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
