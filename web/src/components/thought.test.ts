import { describe, expect, it } from "vitest";

import { splitThought, THOUGHT_HEAD_MAX, THOUGHT_HEAD_MAX_LATIN } from "./thought";

/**
 * 这个函数决定的是读者扫一列推理时**看见的那一行**。钉的是两件事：句子边界优先
 * 于字数，以及英文找不到句号时退到词边界而不是切在词中间——后者是实测切出
 * `Redis dis` 之后补的。
 */
describe("splitThought", () => {
  it("汉字：第一个句号就是第一句，其余是正文", () => {
    const { head, body } = splitThought("先看一下这个文件。然后决定改一段还是重写。");
    expect(head).toBe("先看一下这个文件。");
    expect(body).toBe("然后决定改一段还是重写。");
  });

  it("模型自己的换行先于一切：第一行是它给自己写的标题", () => {
    const { head, body } = splitThought("计划\n1. 读文件。2. 改一段。");
    expect(head).toBe("计划");
    expect(body).toBe("1. 读文件。2. 改一段。");
  });

  it("汉字没有句号时在 120 个字上切", () => {
    const text = "汉".repeat(300);
    const { head, body } = splitThought(text);
    expect(head).toHaveLength(THOUGHT_HEAD_MAX);
    expect(body).toHaveLength(300 - THOUGHT_HEAD_MAX);
  });

  it("英文：第一句超过 120 个字符也整句保留，上限是 200", () => {
    const first =
      "The user wants me to delegate three independent analyses to analyst sub-agents, one per technical approach (A: Redis distributed lock).";
    const { head, body } = splitThought(`${first} Let me craft three briefs.`);
    expect(first.length).toBeGreaterThan(THOUGHT_HEAD_MAX);
    expect(first.length).toBeLessThan(THOUGHT_HEAD_MAX_LATIN);
    expect(head).toBe(first);
    expect(body).toBe("Let me craft three briefs.");
  });

  it("英文找不到句号时退到最后一个词边界，不切在词中间", () => {
    const words = Array.from({ length: 60 }, (_, index) => `word${String(index)}`);
    const text = words.join(" ");
    const { head, body } = splitThought(text);
    expect(head.length).toBeLessThanOrEqual(THOUGHT_HEAD_MAX_LATIN);
    // 摘要以一个完整的词结尾，正文以一个完整的词开头。
    expect(words).toContain(head.split(" ").at(-1));
    expect(words).toContain(body.split(" ")[0]);
    expect(`${head} ${body}`).toBe(text);
  });

  it("`notes.md` 和 `0.5` 不会被当成句号", () => {
    const { head, body } = splitThought(
      "Read notes.md first, it holds 0.5 of the plan. Then write.",
    );
    expect(head).toBe("Read notes.md first, it holds 0.5 of the plan.");
    expect(body).toBe("Then write.");
  });

  it("夹着一个英文文件名的汉字推理仍按汉字的尺量", () => {
    const text = `先看看 notes.md 里有什么${"，再看别的".repeat(30)}`;
    const { head } = splitThought(text);
    // 没有句号，切在 120 个字上，而不是 200。
    expect(head).toHaveLength(THOUGHT_HEAD_MAX);
  });

  it("短到只有一句的没有正文", () => {
    expect(splitThought("先读一下。")).toEqual({ head: "先读一下。", body: "" });
  });
});
