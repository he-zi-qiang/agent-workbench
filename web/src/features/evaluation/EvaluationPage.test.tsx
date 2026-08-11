import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvaluationPage } from "./EvaluationPage";
import {
  REPORTS,
  SELF_DISAGREEMENT,
  hits,
  percent,
  reportsByGoldSet,
} from "./reports";

describe("EvaluationPage", () => {
  // The page may show retrieval scores, because those are read out of
  // evals/rag/reports/*.json rather than written by hand. What it may not do is
  // show a number the repository cannot back, or show the retrieval table
  // without the reason that table cannot rank the two paths.
  it("shows the measured retrieval scores that are actually in the reports", () => {
    render(<EvaluationPage />);

    const tables = screen.getAllByRole("table");
    const shown = tables.map((table) => table.textContent ?? "").join("\n");
    for (const row of REPORTS) {
      const { recall_at_1, recall_at_3 } = row.report.scores;
      expect(shown).toContain(
        `${hits(recall_at_1, row.report.question_count)} / ${row.report.question_count} 题`,
      );
      expect(shown).toContain(percent(recall_at_1));
      expect(shown).toContain(percent(recall_at_3));
    }
  });

  it("never puts two gold sets in one table", () => {
    render(<EvaluationPage />);

    const groups = reportsByGoldSet();
    // One table per question set. Four rows under one heading read as a
    // ranking however the surrounding prose is worded -- and on these reports
    // that reading is backwards, because the path with the higher score is
    // the one still being scored on the older, smaller set.
    expect(screen.getAllByRole("table")).toHaveLength(groups.length);
    for (const group of groups) {
      const table = screen.getByRole("table", {
        name:
          groups.length === 1
            ? "检索评测结果"
            : `检索评测结果（题库 ${group.digest}）`,
      });
      const text = table.textContent ?? "";
      // Every count in this table is over this group's own denominator.
      const denominators = [...text.matchAll(/\/ (\d+) 题/g)].map((match) =>
        Number(match[1]),
      );
      expect(denominators.length).toBeGreaterThan(0);
      expect(new Set(denominators)).toEqual(new Set([group.questionCount]));
    }
  });

  it("refuses to let the table be read as a ranking of the two paths", () => {
    const { container } = render(<EvaluationPage />);

    expect(screen.getByText("哪条实现路径更好")).toBeInTheDocument();
    expect(container.textContent).toContain("测量误差");
    // Divided by the 38-question set these were measured on. Printing them
    // over the current 52-question reference set restated a measurement as a
    // stronger one nobody made.
    expect(SELF_DISAGREEMENT.questionCount).toBe(38);
    expect(container.textContent).toContain(
      `自研检索 ${SELF_DISAGREEMENT.reference}/${SELF_DISAGREEMENT.questionCount} 题`,
    );
    expect(container.textContent).toContain(
      `LlamaIndex ${SELF_DISAGREEMENT.llamaIndex}/${SELF_DISAGREEMENT.questionCount} 题`,
    );
    expect(container.textContent).not.toContain(
      `自研检索 ${SELF_DISAGREEMENT.reference}/52 题`,
    );
  });

  it("publishes no answer-quality score, because no runner has produced one", () => {
    const { container } = render(<EvaluationPage />);

    expect(screen.getByText("RAGAS 目前只有配置，没有结果")).toBeInTheDocument();
    expect(container.textContent).toContain(
      "“配置里启用了”不等于“已经跑出结果”，缺的部分不用示例数字补。",
    );
    // Every percentage on the page has to be a retrieval recall from a report.
    const shown = container.textContent?.match(/\d+(?:\.\d+)?%/g) ?? [];
    const measured = new Set(
      REPORTS.flatMap((row) => [
        percent(row.report.scores.recall_at_1),
        percent(row.report.scores.recall_at_3),
      ]),
    );
    expect(shown.length).toBeGreaterThan(0);
    expect(shown.filter((value) => !measured.has(value))).toEqual([]);
  });

  it("keeps the engineering detail collapsed", () => {
    const { container } = render(<EvaluationPage />);

    expect(container.querySelector("details")).not.toHaveAttribute("open");
  });
});
