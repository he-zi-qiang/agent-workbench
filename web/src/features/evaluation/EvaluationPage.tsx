import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  FileSearch,
  FlaskConical,
  Target,
} from "lucide-react";
import {
  REPORTS,
  SELF_DISAGREEMENT,
  hits,
  percent,
  reportsByGoldSet,
  reportsShareOneGoldSet,
  selfDisagreementCoversAShownReport,
} from "./reports";

export function EvaluationPage() {
  const comparable = reportsShareOneGoldSet();
  const goldSets = reportsByGoldSet();

  return (
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">效果评测</span>
          <h1>找资料，找得准吗？</h1>
          <p>
            这一页只回答一件事：给一个问题，系统能不能把正确的那份文档排在最前面。
            下面的数字直接来自仓库里的评测报告，没有人工填写。
          </p>
        </div>
        <div className="aw-page-note">
          <FlaskConical aria-hidden="true" size={17} />
          <span>离线评测，不影响你在 Chat 里的使用</span>
        </div>
      </header>

      <section className="aw-card aw-section" aria-labelledby="method-title">
        <div className="aw-card-header">
          <div>
            <span className="aw-eyebrow">怎么测的</span>
            <h2 id="method-title">先出题，再看排名</h2>
          </div>
          <Target aria-hidden="true" size={20} />
        </div>
        <p className="aw-eval-method">
          事先准备好一批题目，每道题都标好了“正确答案应该来自哪份文档”。
          让系统去检索，然后看正确文档有没有出现在结果里、排在第几位。
          排得越靠前越好。
          {goldSets.length === 1
            ? ` 当前这批共 ${goldSets[0]?.questionCount ?? 0} 道题。`
            : " 题库改过一次，所以下面按题库分开列，每组各有自己的题量。"}
        </p>
        <div className="aw-eval-legend">
          <div>
            <strong>第一条就找对</strong>
            <span>返回的第 1 条就是正确文档。最严格的一档。</span>
          </div>
          <div>
            <strong>前三条内找对</strong>
            <span>正确文档出现在前 3 条里。实际使用中够用。</span>
          </div>
          <div>
            <strong>单次检索耗时</strong>
            <span>检索这一步花的时间，不含模型写答案的时间。</span>
          </div>
        </div>
      </section>

      <section className="aw-card aw-section" aria-labelledby="scores-title">
        <div className="aw-card-header">
          <div>
            <span className="aw-eyebrow">结果</span>
            <h2 id="scores-title">四种配置在同一批题目上的表现</h2>
          </div>
          <FileSearch aria-hidden="true" size={20} />
        </div>

        {comparable ? null : (
          <div className="aw-notice is-warning">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>
              这几份报告不是在同一批题目上跑出来的，所以<strong>跨组的数字不能相互比较</strong>。
              题库换过之后只有自研检索重跑了，LlamaIndex 那条路径还停在旧题库上——
              它看起来分数更高，是因为题目更少更旧，不是因为它更准。要横向对比，
              得把两条路径都放到同一批题目上重跑。
            </span>
          </div>
        )}

        {goldSets.map((group) => (
          <div className="aw-eval-group" key={group.digest}>
            {comparable ? null : (
              <p className="aw-page-note">
                题库指纹 {group.digest} · 共 {group.questionCount} 道题 ·
                只有这一组内部可以互相比较
              </p>
            )}
            <div
              className="aw-eval-table"
              role="table"
              aria-label={
                comparable
                  ? "检索评测结果"
                  : `检索评测结果（题库 ${group.digest}）`
              }
            >
              <div className="aw-eval-row is-heading" role="row">
                <span role="columnheader">检索方式</span>
                <span role="columnheader">实现路径</span>
                <span role="columnheader">第一条就找对</span>
                <span role="columnheader">前三条内找对</span>
                <span role="columnheader">单次检索耗时</span>
              </div>
              {group.rows.map((row) => (
                <div className="aw-eval-row" key={row.file} role="row">
                  <strong role="cell">{row.retrievalLabel}</strong>
                  <span role="cell">{row.pathLabel}</span>
                  <span role="cell">
                    <b>
                      {hits(row.report.scores.recall_at_1, row.report.question_count)} /{" "}
                      {row.report.question_count} 题
                    </b>
                    <small>{percent(row.report.scores.recall_at_1)}</small>
                  </span>
                  <span role="cell">
                    <b>
                      {hits(row.report.scores.recall_at_3, row.report.question_count)} /{" "}
                      {row.report.question_count} 题
                    </b>
                    <small>{percent(row.report.scores.recall_at_3)}</small>
                  </span>
                  <span role="cell">
                    <b>{Math.round(row.report.scores.retrieval_latency_ms)} ms</b>
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>

      <div className="aw-card-grid">
        <section className="aw-card aw-section" aria-labelledby="can-say-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">这张表能说明</span>
              <h2 id="can-say-title">正确资料基本都在前三条</h2>
            </div>
            <CheckCircle2 aria-hidden="true" size={20} />
          </div>
          <ul className="aw-capability-list">
            <li>
              <CheckCircle2 aria-hidden="true" size={16} />
              <div>
                <strong>每份报告都做到了前三条内命中绝大多数题目</strong>
                <span>
                  在各自的题库里，“关键词 + 语义”都比“纯语义”更稳，代价是慢一些——
                  这个方向在两批题目上一致，是这页最扎实的一条结论。
                </span>
              </div>
            </li>
            <li>
              <CheckCircle2 aria-hidden="true" size={16} />
              <div>
                <strong>题目和报告都在仓库里</strong>
                <span>
                  {goldSets
                    .map((group) => `${group.digest}（${group.questionCount} 题）`)
                    .join("、")}
                  ，任何人都可以重新跑一遍核对。
                </span>
              </div>
            </li>
          </ul>
        </section>

        <section className="aw-card aw-section" aria-labelledby="cannot-say-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">这张表不能说明</span>
              <h2 id="cannot-say-title">哪条实现路径更好</h2>
            </div>
            <AlertTriangle aria-hidden="true" size={20} />
          </div>
          {/* Every figure below is divided by the gold set it was measured on
              (38 questions, digest a26070043b0ffde1), never by whatever the
              current reports answer. Restating a 9/38 result as 9/52 would be
              inventing a stronger measurement than the one that was made. */}
          <p className="aw-eval-method">
            在 {SELF_DISAGREEMENT.questionCount} 题的旧题库（指纹 {SELF_DISAGREEMENT.goldDigest}）上，
            同一条路径连着跑两遍，它自己的结果就会变：
            <strong>
              自研检索 {SELF_DISAGREEMENT.reference}/{SELF_DISAGREEMENT.questionCount} 题
            </strong>
            、
            <strong>
              LlamaIndex {SELF_DISAGREEMENT.llamaIndex}/{SELF_DISAGREEMENT.questionCount} 题
            </strong>
            前后次序不一致。原因是打分并列时没有规定谁排前面。
          </p>
          <div className="aw-notice is-warning">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>
              在那批题目上，两条路径之间只有 {SELF_DISAGREEMENT.betweenPathsTopThree}/
              {SELF_DISAGREEMENT.questionCount} 题的前三名不同，比它们各自的抖动还小 ——
              所以那点差距是测量误差，不是质量差距。默认流量因此没有切换。
              {selfDisagreementCoversAShownReport()
                ? ""
                : " 这组对照来自已经不在上表中的旧报告，仅作为背景保留。"}
            </span>
          </div>
        </section>
      </div>

      <section className="aw-card aw-section" aria-labelledby="not-measured-title">
        <div className="aw-card-header">
          <div>
            <span className="aw-eyebrow">还没有测</span>
            <h2 id="not-measured-title">写出来的答案是否忠于资料</h2>
          </div>
          <CircleDashed aria-hidden="true" size={20} />
        </div>
        <ul className="aw-capability-list">
          <li>
            <CircleDashed aria-hidden="true" size={16} />
            <div>
              <strong>上面测的是“找资料”，不是“写答案”</strong>
              <span>
                找对了资料，模型仍然可能写出与资料不符的句子。这是另一件事，需要另一套评测。
              </span>
            </div>
          </li>
          <li>
            <CircleDashed aria-hidden="true" size={16} />
            <div>
              <strong>RAGAS 目前只有配置，没有结果</strong>
              <span>仓库里还没有 runner，也没有报告，所以这里不会出现回答质量分数。</span>
            </div>
          </li>
        </ul>
        <div className="aw-notice is-warning">
          <AlertTriangle aria-hidden="true" size={16} />
          <span>“配置里启用了”不等于“已经跑出结果”，缺的部分不用示例数字补。</span>
        </div>
      </section>

      <details className="aw-card aw-section">
        <summary>
          <ChevronDown aria-hidden="true" size={14} />
          工程详情
        </summary>
        <ul className="aw-capability-list">
          {REPORTS.map((row) => (
            <li key={row.file}>
              <FlaskConical aria-hidden="true" size={16} />
              <div>
                <strong>
                  evals/rag/reports/{row.file} · MRR {row.report.scores.mrr.toFixed(4)}
                </strong>
                <span>{row.report.index_identity}</span>
              </div>
            </li>
          ))}
        </ul>
        <p className="aw-page-note">
          MRR 是正确文档排名倒数的平均值：全排第 1 得 1.0，全排第 2 得 0.5。
          自一致性数据来自 docs/status.md（2026-08-03）。
        </p>
      </details>
    </main>
  );
}
