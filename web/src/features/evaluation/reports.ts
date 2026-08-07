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
 * Every report answers the same gold set, so a single question count and digest
 * describe all of them. Reading them off the data keeps the page honest if a
 * future run changes the set; disagreement means the reports are no longer
 * comparable and the caller should say so rather than pick one.
 */
export const QUESTION_COUNT = hybridReference.question_count;
export const GOLD_DIGEST = hybridReference.gold_digest;

export function reportsShareOneGoldSet(): boolean {
  return REPORTS.every(
    (row) =>
      row.report.question_count === QUESTION_COUNT &&
      row.report.gold_digest === GOLD_DIGEST,
  );
}

/** A rate over the gold set, restated as the count a reader can picture. */
export function hits(rate: number, total: number): number {
  return Math.round(rate * total);
}

export function percent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}
