// The four candidate retrieval reports, read straight from the files the
// offline evaluator writes. They are imported rather than transcribed so this
// page cannot drift from the repository: if a report is re-run, the page moves
// with it, and if one is deleted the build fails instead of showing a stale
// number. There is no API for these -- they are build-time evidence.
import denseLlamaIndex from "../../../../evals/rag/reports/dense-llama_index.json";
import denseReference from "../../../../evals/rag/reports/dense-reference.json";
import hybridLlamaIndex from "../../../../evals/rag/reports/hybrid-llama_index.json";
import hybridReference from "../../../../evals/rag/reports/hybrid-reference.json";

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

export const REPORTS: readonly ReportRow[] = [
  {
    file: "hybrid-reference.json",
    retrievalLabel: "关键词 + 语义",
    pathLabel: "自研检索",
    report: hybridReference,
  },
  {
    file: "hybrid-llama_index.json",
    retrievalLabel: "关键词 + 语义",
    pathLabel: "LlamaIndex",
    report: hybridLlamaIndex,
  },
  {
    file: "dense-reference.json",
    retrievalLabel: "纯语义",
    pathLabel: "自研检索",
    report: denseReference,
  },
  {
    file: "dense-llama_index.json",
    retrievalLabel: "纯语义",
    pathLabel: "LlamaIndex",
    report: denseLlamaIndex,
  },
];

/**
 * One gold set, and the reports that answered it.
 *
 * The page groups by this rather than laying all four reports in one table.
 * They do not currently answer the same questions -- the reference path was
 * re-run on a 52-question set and the LlamaIndex path was not -- and four rows
 * side by side read as a ranking whatever warning sits above them. On these
 * numbers that reading is backwards: LlamaIndex looks ~9 points better at
 * recall@1 purely because it was scored on the older, smaller set.
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
export function reportsByGoldSet(): readonly GoldSetGroup[] {
  const groups = new Map<string, ReportRow[]>();
  for (const row of REPORTS) {
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

export function reportsShareOneGoldSet(): boolean {
  return reportsByGoldSet().length === 1;
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
export function selfDisagreementCoversAShownReport(): boolean {
  return REPORTS.some(
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
