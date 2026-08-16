import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
// The four committed reports, still imported here. The page reads them over
// HTTP now, but a test that made its own numbers up would let the page drift
// from the repository -- which is exactly what the old build-time import was
// protecting against. So they stay, as the fixture the fake API serves.
import denseLlamaIndex from "../../../../evals/rag/reports/dense-llama_index.json";
import denseReference from "../../../../evals/rag/reports/dense-reference.json";
import hybridLlamaIndex from "../../../../evals/rag/reports/hybrid-llama_index.json";
import hybridReference from "../../../../evals/rag/reports/hybrid-reference.json";
import {
  getEvaluationReports,
  getEvaluationRun,
  startEvaluationRun,
} from "../../api/client";
import type { EvaluationReportView } from "../../api/types";
import { EvaluationPage } from "./EvaluationPage";
import {
  SELF_DISAGREEMENT,
  hits,
  percent,
  retrievalReports,
} from "./reports";

vi.mock("../../api/client", () => ({
  getEvaluationReports: vi.fn(),
  getEvaluationRun: vi.fn(() => Promise.resolve({ run: null })),
  startEvaluationRun: vi.fn(),
  cancelEvaluationRun: vi.fn(),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: () => ({
    identity: { tenantId: "t", principalId: "p", scopes: [] },
  }),
}));

const VIEWS: EvaluationReportView[] = [
  { suite: "rag", name: "hybrid-reference", payload: hybridReference },
  { suite: "rag", name: "hybrid-llama_index", payload: hybridLlamaIndex },
  { suite: "rag", name: "dense-reference", payload: denseReference },
  { suite: "rag", name: "dense-llama_index", payload: denseLlamaIndex },
];

const ROWS = retrievalReports(VIEWS);

function mounted() {
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queries}>
      <EvaluationPage />
    </QueryClientProvider>,
  );
}

/** Wait for the reports to land; every assertion below is about what they say. */
async function loaded() {
  const view = mounted();
  await screen.findAllByRole("table");
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getEvaluationReports).mockResolvedValue({
    reports: VIEWS,
    runs_enabled: false,
    how_to_run: { rag: "uv run --extra embedding python scripts/run_rag_eval.py" },
  });
  vi.mocked(getEvaluationRun).mockResolvedValue({ run: null });
});

describe("EvaluationPage", () => {
  // The page may show retrieval scores, because those come from
  // evals/rag/reports/*.json rather than being written by hand. What it may not
  // do is show a number the repository cannot back, or show the retrieval table
  // without the reason that table cannot rank the two paths.
  it("shows the measured retrieval scores that are actually in the reports", async () => {
    await loaded();

    const tables = screen.getAllByRole("table");
    const shown = tables.map((table) => table.textContent ?? "").join("\n");
    for (const row of ROWS) {
      const { recall_at_1, recall_at_3 } = row.report.scores;
      expect(shown).toContain(
        `${hits(recall_at_1, row.report.question_count)} / ${row.report.question_count} 题`,
      );
      expect(shown).toContain(percent(recall_at_1));
      expect(shown).toContain(percent(recall_at_3));
    }
  });

  it("never puts two gold sets in one table", async () => {
    await loaded();

    // Counted from the fixture, not from `reportsByGoldSet`. Deriving the
    // expectation with the same function the page uses made this a tautology:
    // a grouping that collapsed everything into one bucket moved both sides
    // together and the assertion stayed green. Found by sabotage.
    const digests = new Set(
      VIEWS.map((view) => String(view.payload.gold_digest)),
    );

    // One table per question set. Four rows under one heading read as a
    // ranking however the surrounding prose is worded -- and on these reports
    // that reading is backwards, because the path with the higher score is
    // the one still being scored on the older, smaller set.
    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(digests.size);
    for (const table of tables) {
      const text = table.textContent ?? "";
      // Every count in this table is over one denominator -- whichever it is.
      const denominators = [...text.matchAll(/\/ (\d+) 题/g)].map((match) =>
        Number(match[1]),
      );
      expect(denominators.length).toBeGreaterThan(0);
      expect(new Set(denominators).size).toBe(1);
    }
    // And between them the tables cover every distinct question count in the
    // fixture, so a page that dropped a group would fail here rather than
    // quietly showing fewer reports than exist.
    const counts = new Set(
      tables.flatMap((table) =>
        [...(table.textContent ?? "").matchAll(/\/ (\d+) 题/g)].map((match) =>
          Number(match[1]),
        ),
      ),
    );
    expect(counts).toEqual(
      new Set(VIEWS.map((view) => Number(view.payload.question_count))),
    );
  });

  it("splits the table when two question sets are on the page", async () => {
    // A synthetic second gold set, because the four committed reports are all
    // on one now. The grouping is the page's defence against four rows reading
    // as a ranking, and with today's data that defence is never exercised --
    // so this fixture exercises it, and the assertions above keep the page
    // tied to what the repository actually measured.
    vi.mocked(getEvaluationReports).mockResolvedValue({
      reports: [
        VIEWS[0] as EvaluationReportView,
        {
          suite: "rag",
          name: "hybrid-llama_index",
          payload: {
            ...hybridLlamaIndex,
            gold_digest: "a26070043b0ffde1",
            question_count: 38,
          },
        },
      ],
      runs_enabled: false,
      how_to_run: {},
    });

    await loaded();

    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(2);
    for (const table of tables) {
      const denominators = new Set(
        [...(table.textContent ?? "").matchAll(/\/ (\d+) 题/g)].map((match) =>
          Number(match[1]),
        ),
      );
      expect(denominators.size).toBe(1);
    }
    // Each table names its own question set, so the two cannot be read as one.
    expect(
      screen.getByRole("table", { name: /题库 55ec24c7d2b86062/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: /题库 a26070043b0ffde1/ }),
    ).toBeInTheDocument();
  });

  it("refuses to let the table be read as a ranking of the two paths", async () => {
    const { container } = await loaded();

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

  it("publishes no answer-quality score, because no runner has produced one", async () => {
    const { container } = await loaded();

    expect(screen.getByText("RAGAS 目前只有配置，没有结果")).toBeInTheDocument();
    expect(container.textContent).toContain(
      "“配置里启用了”不等于“已经跑出结果”，缺的部分不用示例数字补。",
    );
    // Every percentage on the page has to be a retrieval recall from a report.
    const shown = container.textContent?.match(/\d+(?:\.\d+)?%/g) ?? [];
    const measured = new Set(
      ROWS.flatMap((row) => [
        percent(row.report.scores.recall_at_1),
        percent(row.report.scores.recall_at_3),
      ]),
    );
    expect(shown.length).toBeGreaterThan(0);
    expect(shown.filter((value) => !measured.has(value))).toEqual([]);
  });

  it("keeps the engineering detail collapsed", async () => {
    const { container } = await loaded();

    expect(container.querySelector("details")).not.toHaveAttribute("open");
  });

  it("gives a deployment that cannot run one the command instead of a button", async () => {
    await loaded();

    // Not a disabled button. This deployment cannot start a run at all, and a
    // greyed-out control would say "later" where the truth is "elsewhere".
    expect(
      screen.queryByRole("button", { name: /检索消融/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/scripts\/run_rag_eval\.py/)).toBeInTheDocument();
  });

  it("starts the suite whose button was pressed", async () => {
    const user = userEvent.setup();
    vi.mocked(getEvaluationReports).mockResolvedValue({
      reports: VIEWS,
      runs_enabled: true,
      how_to_run: {},
    });
    vi.mocked(startEvaluationRun).mockResolvedValue({
      suite: "triage",
      status: "running",
      started_at: new Date().toISOString(),
      finished_at: null,
      exit_code: null,
      recent_output: [],
    });

    await loaded();
    await user.click(await screen.findByRole("button", { name: /任务分流/ }));

    await waitFor(() => {
      expect(vi.mocked(startEvaluationRun).mock.calls[0]?.[1]).toBe("triage");
    });
  });

  it("says how long a live run has been going, and that it takes tens of minutes", async () => {
    vi.mocked(getEvaluationReports).mockResolvedValue({
      reports: VIEWS,
      runs_enabled: true,
      how_to_run: {},
    });
    vi.mocked(getEvaluationRun).mockResolvedValue({
      run: {
        suite: "rag",
        status: "running",
        started_at: new Date(Date.now() - 12 * 60_000).toISOString(),
        finished_at: null,
        exit_code: null,
        recent_output: ["indexing corpus", "measuring hybrid"],
      },
    });

    await loaded();

    // A bare spinner cannot be told apart from a hang, and this one runs for
    // tens of minutes -- so the page says how long it has been and how long
    // these take.
    const live = await screen.findByText(/已经 12 分钟/);
    // Scoped to the live panel: the suite button carries the same figure, and
    // a document-wide match would pass on the button alone -- which says how
    // long these take without saying anything about the run in front of you.
    expect(live).toHaveTextContent("30–70 分钟");
    expect(screen.getByText(/measuring hybrid/)).toBeInTheDocument();
  });
});
