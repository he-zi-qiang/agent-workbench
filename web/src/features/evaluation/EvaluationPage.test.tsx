import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
// The four committed reports, still imported here. The page reads them over
// HTTP now, but a test that made its own numbers up would let the page drift
// from the repository -- which is exactly what the old build-time import was
// protecting against. So they stay, as the fixture the fake API serves.
import chatHybrid from "../../../../evals/chat/reports/chat-hybrid-180s.json";
import denseLlamaIndex from "../../../../evals/rag/reports/dense-llama_index.json";
import denseReference from "../../../../evals/rag/reports/dense-reference.json";
import hybridLlamaIndex from "../../../../evals/rag/reports/hybrid-llama_index.json";
import hybridReference from "../../../../evals/rag/reports/hybrid-reference.json";
import triageReport from "../../../../evals/triage/reports/report.json";
import {
  getEvaluationReports,
  getEvaluationRun,
  startEvaluationRun,
} from "../../api/client";
import type { EvaluationReportView } from "../../api/types";
import { EvaluationPage } from "./EvaluationPage";
import {
  SELF_DISAGREEMENT,
  chatReports,
  hits,
  percent,
  retrievalReports,
  triageReports,
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

const RAG_VIEWS: EvaluationReportView[] = [
  { suite: "rag", name: "hybrid-reference", payload: hybridReference },
  { suite: "rag", name: "hybrid-llama_index", payload: hybridLlamaIndex },
  { suite: "rag", name: "dense-reference", payload: denseReference },
  { suite: "rag", name: "dense-llama_index", payload: denseLlamaIndex },
];

// The other two suites the API serves, and the page rendered nothing of until
// 2026-08-31. Same principle as above: the committed reports are the fixture,
// so a page that stops agreeing with the repository fails here.
const OTHER_VIEWS: EvaluationReportView[] = [
  { suite: "triage", name: "report", payload: triageReport },
  { suite: "chat", name: "chat-hybrid-180s", payload: chatHybrid },
];

const VIEWS: EvaluationReportView[] = [...RAG_VIEWS, ...OTHER_VIEWS];

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
    // `RAG_VIEWS`, not `VIEWS`: the triage report carries a `gold_digest` of
    // its own and is not a retrieval report, so counting it here would demand a
    // fifth retrieval table that should not exist.
    const digests = new Set(
      RAG_VIEWS.map((view) => String(view.payload.gold_digest)),
    );

    // One table per question set. Four rows under one heading read as a
    // ranking however the surrounding prose is worded -- and on these reports
    // that reading is backwards, because the path with the higher score is
    // the one still being scored on the older, smaller set.
    const tables = screen.getAllByRole("table", { name: /检索评测结果/ });
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
      new Set(RAG_VIEWS.map((view) => Number(view.payload.question_count))),
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

  it("prints no percentage that is not in a served report", async () => {
    const { container } = await loaded();

    expect(screen.getByText("RAGAS 只有配置，没有依赖也没有报告")).toBeInTheDocument();
    expect(container.textContent).toContain(
      "“配置里启用了”不等于“已经跑出结果”，缺的部分不用示例数字补。",
    );
    // Every percentage on the page has to come from a report the API served.
    // Widened on 2026-08-31 along with the page: it used to allow only
    // retrieval recalls, which is why it caught the triage and chat tables the
    // moment they landed -- correctly, the assertion just had a narrower idea
    // of "measured" than the page now does.
    const shown = container.textContent?.match(/\d+(?:\.\d+)?%/g) ?? [];
    const measured = new Set([
      ...ROWS.flatMap((row) => [
        percent(row.report.scores.recall_at_1),
        percent(row.report.scores.recall_at_3),
      ]),
      ...triageReports(VIEWS).flatMap((report) => [
        percent(report.accuracy),
        ...report.byClass.map((entry) =>
          entry.total === 0 ? "—" : percent(entry.correct / entry.total),
        ),
      ]),
      ...chatReports(VIEWS).flatMap((report) =>
        report.arms.flatMap((arm) => [
          percent(arm.factRecall),
          percent(arm.citationPrecision),
          percent(arm.citationRecall),
        ]),
      ),
    ]);
    expect(shown.length).toBeGreaterThan(0);
    // `textContent` runs adjacent cells together ("2/2" + "100.0%" reads as
    // "2100.0%"), so a match is allowed to be a measured value with digits
    // glued to its front. Nothing weaker would pass; nothing stronger can be
    // asserted about a string with no cell boundaries in it.
    const unmeasured = shown.filter(
      (value) => ![...measured].some((known) => value.endsWith(known)),
    );
    expect(unmeasured).toEqual([]);
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

  // 三个可点的 suite、后端读三个目录、仓库里躺着三类报告，而页面只渲染 rag
  // 一种。跑完「回答质量」页面一个字都不变，且空态说的是「这台机器还没有跑过
  // 评测」——对刚跑完的那台机器，那是假话。
  it("renders the triage report the API served, per class as well as overall", async () => {
    await loaded();

    const table = await screen.findByRole("table", { name: /任务分流评测结果/ });
    // 整体 0.8333 —— 而它是「两类满分、一类全错」，逐类那几行才说得出这件事。
    expect(table).toHaveTextContent("20 / 24 题");
    expect(table).toHaveTextContent("83.3%");
    expect(table).toHaveTextContent("unsure");
    expect(table).toHaveTextContent("0 / 4 题");
  });

  it("renders the chat report, including the absolute count of fabricated citations", async () => {
    await loaded();

    const table = await screen.findByRole("table", { name: /回答质量评测结果/ });
    expect(table).toHaveTextContent("fixed");
    expect(table).toHaveTextContent("11 / 13 题");
    // 一条绝对计数，不是比率：一条编造的引用就够坏，把它化成百分比会让它消失。
    expect(table).toHaveTextContent("干净拒答 2/2");
  });

  it("says which category is missing rather than that nothing was ever run", async () => {
    vi.mocked(getEvaluationReports).mockResolvedValue({
      reports: OTHER_VIEWS,
      runs_enabled: false,
      how_to_run: {},
    });

    mounted();

    expect(await screen.findByText("还没有检索消融的报告")).toBeInTheDocument();
    expect(
      screen.queryByText("这台机器还没有跑过评测"),
    ).not.toBeInTheDocument();
  });

  it("still says nothing was run when the API served nothing at all", async () => {
    vi.mocked(getEvaluationReports).mockResolvedValue({
      reports: [],
      runs_enabled: false,
      how_to_run: {},
    });

    mounted();

    expect(
      await screen.findByText("这台机器还没有跑过评测"),
    ).toBeInTheDocument();
  });
});
