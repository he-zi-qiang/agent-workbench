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
