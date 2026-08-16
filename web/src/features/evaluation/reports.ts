// The candidate retrieval reports, read from the API rather than imported at
// build time.
//
// They used to be four static imports, and the argument for that was real: a
// deleted report broke `tsc` instead of showing a stale number. That check has
// not been dropped, it has moved -- `tests/api/test_evaluation_api.py` asserts
// the four committed reports still parse. What the imports could not do is show
// a report produced by a run somebody started from this page, which is the
// whole point of the page now.
//
// Every derivation below is unchanged; only where the rows come from moved.
import type { EvaluationReportView } from "../../api/types";

export interface RetrievalReport {
  gold_digest: string;
  index_identity: string;
  question_count: number;
  scores: {
    mrr: number;
    recall_at_1: number;
    recall_at_3: number;
    retrieval_latency_ms: number;
  };
}

export interface ReportRow {
  file: string;
  /** How candidates are proposed, in the reader's words rather than the config's. */
  retrievalLabel: string;
  /** Which implementation proposed them. */
  pathLabel: string;
  report: RetrievalReport;
}

/**
 * How a report's filename reads in the page's own words.
 *
 * Hand-written because the runner does not record it: `hybrid-reference` is a
 * file naming convention, and "关键词 + 语义 · 自研检索" is what it means. A
 * report whose name is not in here is still shown -- with its raw name, which
 * is honest -- rather than hidden, because hiding it is how a new arm produced
 * by a run somebody just started would silently fail to appear.
 */
const LABELS: Record<string, { retrievalLabel: string; pathLabel: string }> = {
  "hybrid-reference": { retrievalLabel: "关键词 + 语义", pathLabel: "自研检索" },
  "hybrid-llama_index": { retrievalLabel: "关键词 + 语义", pathLabel: "LlamaIndex" },
  "dense-reference": { retrievalLabel: "纯语义", pathLabel: "自研检索" },
  "dense-llama_index": { retrievalLabel: "纯语义", pathLabel: "LlamaIndex" },
};

/** The retrieval reports among what the API returned, in a stable order. */
export function retrievalReports(
  views: readonly EvaluationReportView[],
): readonly ReportRow[] {
  const order = Object.keys(LABELS);
  return views
    .filter((view) => view.suite === "rag" && isRetrievalReport(view.payload))
    .map((view) => ({
      file: `${view.name}.json`,
      retrievalLabel: LABELS[view.name]?.retrievalLabel ?? view.name,
      // The filename, when this file has no words for it. `graph-ablation-hybrid`
      // says more about itself than "未命名" would, and a reader who sees it can
      // go and look; a row hidden for want of a label could not be looked for.
      pathLabel: LABELS[view.name]?.pathLabel ?? "—",
      report: view.payload as unknown as RetrievalReport,
    }))
    .sort((left, right) => {
      // Known names first, in the order this file lists them, so the table
      // does not reshuffle when a new arm lands. Everything else after, by
      // name, so the order is total and a test can assert it.
      const leftIndex = order.indexOf(left.file.replace(/\.json$/, ""));
      const rightIndex = order.indexOf(right.file.replace(/\.json$/, ""));
      if (leftIndex !== rightIndex) {
        return (leftIndex < 0 ? order.length : leftIndex) -
          (rightIndex < 0 ? order.length : rightIndex);
      }
      return left.file.localeCompare(right.file);
    });
}

/**
 * Whether this payload carries what the table below reads.
 *
 * The API passes a runner's object through whole and promises only that it has
 * a `scores` mapping. A retrieval report has more than that, and a graph
 * ablation report in the same directory has different fields again -- so the
 * page checks rather than assumes, and a report it cannot render is left out
 * of the table instead of rendering as `undefined`.
 */
function isRetrievalReport(payload: Record<string, unknown>): boolean {
  const scores = payload.scores;
  return (
    typeof payload.gold_digest === "string" &&
    typeof payload.question_count === "number" &&
    typeof scores === "object" &&
    scores !== null &&
    typeof (scores as Record<string, unknown>).mrr === "number" &&
    typeof (scores as Record<string, unknown>).recall_at_1 === "number" &&
    typeof (scores as Record<string, unknown>).recall_at_3 === "number"
  );
}

/**
 * One gold set, and the reports that answered it.
 *
 * The page groups by this rather than laying every report in one table. Four
 * rows side by side read as a ranking whatever warning sits above them, and
 * when the reports answer different question sets that reading is not merely
 * unsupported but backwards -- a path scored on an older, smaller set looked
 * ~9 points better at recall@1 for that reason alone.
 *
 * As of 2026-08-15 all four committed reports answer one set, so this collapses
 * to a single table and the split is not currently visible. It is kept, and
 * tested against a synthetic second set, because the situation it exists for is
 * one re-run away: whoever re-runs one arm and not the others creates it again.
 */
export interface GoldSetGroup {
  digest: string;
  questionCount: number;
  rows: readonly ReportRow[];
}

/**
 * The reports grouped by the question set they answered, largest group first.
 *
 * Derived rather than declared, so a re-run that puts every path back on one
 * gold set collapses this to a single group and the page becomes one table
 * again with no edit here.
 */
export function reportsByGoldSet(
  reports: readonly ReportRow[],
): readonly GoldSetGroup[] {
  const groups = new Map<string, ReportRow[]>();
  for (const row of reports) {
    const existing = groups.get(row.report.gold_digest);
    if (existing === undefined) groups.set(row.report.gold_digest, [row]);
    else existing.push(row);
  }
  return [...groups.entries()]
    .map(([digest, rows]) => ({
      digest,
      // Every report carrying a digest answered the set that digest names, so
      // any row's count describes the group.
      questionCount: rows[0]?.report.question_count ?? 0,
      rows,
    }))
    .sort((left, right) => right.rows.length - left.rows.length);
}

export function reportsShareOneGoldSet(
  reports: readonly ReportRow[],
): boolean {
  return reportsByGoldSet(reports).length === 1;
}

/**
 * Repeat-run disagreement, recorded in docs/status.md (2026-08-03).
 *
 * Carried with the gold set it was measured on rather than divided by whatever
 * the current reports happen to answer. It was 9 of *38* questions; printing it
 * over today's 52-question reference set restated a measurement as a stronger
 * one nobody made, and would keep doing so after every future re-run.
 */
export const SELF_DISAGREEMENT = {
  goldDigest: "a26070043b0ffde1",
  questionCount: 38,
  reference: 9,
  llamaIndex: 10,
  betweenPathsTopThree: 3,
} as const;

/** Whether any report on the page still answers the set those figures describe. */
export function selfDisagreementCoversAShownReport(
  reports: readonly ReportRow[],
): boolean {
  return reports.some(
    (row) => row.report.gold_digest === SELF_DISAGREEMENT.goldDigest,
  );
}

/** A rate over the gold set, restated as the count a reader can picture. */
export function hits(rate: number, total: number): number {
  return Math.round(rate * total);
}

export function percent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}
