import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvaluationPage } from "./EvaluationPage";
import { QUESTION_COUNT, REPORTS, hits, percent } from "./reports";

describe("EvaluationPage", () => {
  // The page may show retrieval scores, because those are read out of
  // evals/rag/reports/*.json rather than written by hand. What it may not do is
  // show a number the repository cannot back, or show the retrieval table
  // without the reason that table cannot rank the two paths.
  it("shows the measured retrieval scores that are actually in the reports", () => {
    render(<EvaluationPage />);

    const table = screen.getByRole("table", { name: "检索评测结果" });
    for (const row of REPORTS) {
      const { recall_at_1, recall_at_3 } = row.report.scores;
      expect(
        within(table).getAllByText(
          `${hits(recall_at_1, row.report.question_count)} / ${row.report.question_count} 题`,
        ).length,
      ).toBeGreaterThan(0);
      expect(within(table).getAllByText(percent(recall_at_1)).length).toBeGreaterThan(0);
      expect(within(table).getAllByText(percent(recall_at_3)).length).toBeGreaterThan(0);
    }
  });

  it("refuses to let the table be read as a ranking of the two paths", () => {
    const { container } = render(<EvaluationPage />);

    expect(screen.getByText("哪条实现路径更好")).toBeInTheDocument();
    expect(container.textContent).toContain("测量误差");
    expect(container.textContent).toContain(`自研检索 9/${QUESTION_COUNT} 题`);
    expect(container.textContent).toContain(`LlamaIndex 10/${QUESTION_COUNT} 题`);
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
