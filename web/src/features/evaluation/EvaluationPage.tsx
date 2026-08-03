import {
  BarChart3,
  CheckCircle2,
  CircleDashed,
  FlaskConical,
  Layers3,
  ShieldAlert,
} from "lucide-react";

export function EvaluationPage() {
  return (
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">Evidence, not decoration</span>
          <h1>评测基线</h1>
          <p>这里区分仓库已经具备的评测证据与 Resume v1 明确要完成的目标。</p>
        </div>
        <div className="aw-page-note">
          <FlaskConical aria-hidden="true" size={17} />
          <span>没有报告查询 API，就不在页面上手抄分数。</span>
        </div>
      </header>

      <div className="aw-notice is-warning">
        <ShieldAlert aria-hidden="true" size={16} />
        <span>
          本页不展示示例分数或历史截图。只有携带 dataset digest、index identity、模型与提示词 revision
          的真实运行报告，才可以进入结果面板。
        </span>
      </div>

      <div className="aw-card-grid">
        <section className="aw-card aw-section" aria-labelledby="current-evidence-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Current</span>
              <h2 id="current-evidence-title">当前代码证据</h2>
            </div>
            <BarChart3 aria-hidden="true" size={20} />
          </div>
          <ul className="aw-capability-list">
            <li>
              <CheckCircle2 aria-hidden="true" size={16} />
              <div>
                <strong>确定性检索 Runner</strong>
                <span>已经定义 Recall@1、Recall@3、MRR 和检索延迟中位数。</span>
              </div>
            </li>
            <li>
              <CheckCircle2 aria-hidden="true" size={16} />
              <div>
                <strong>可比性元数据</strong>
                <span>报告记录 gold set digest、index identity 和问题数量。</span>
              </div>
            </li>
            <li>
              <CircleDashed aria-hidden="true" size={16} />
              <div>
                <strong>结果展示</strong>
                <span>当前 API 没有发布评测报告，因此本页没有可验证的数值。</span>
              </div>
            </li>
          </ul>
        </section>

        <section className="aw-card aw-section" aria-labelledby="target-baseline-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Resume v1 target</span>
              <h2 id="target-baseline-title">锁定的目标基线</h2>
            </div>
            <Layers3 aria-hidden="true" size={20} />
          </div>
          <ul className="aw-capability-list">
            <li>
              <CircleDashed aria-hidden="true" size={16} />
              <div>
                <strong>LlamaIndex：确定的 RAG 框架</strong>
                <span>
                  用于 ingestion connector、Node parsing 与 Retriever Adapter；不接管最终回答和 Agent Tool Loop。
                </span>
              </div>
            </li>
            <li>
              <CircleDashed aria-hidden="true" size={16} />
              <div>
                <strong>RAGAS：待接入的回答质量基线</strong>
                <span>
                  目标指标包含 Faithfulness、Answer Relevancy、Context Precision、Context Recall 和 Factual Correctness。
                </span>
              </div>
            </li>
            <li>
              <CircleDashed aria-hidden="true" size={16} />
              <div>
                <strong>统一证据</strong>
                <span>将 RAGAS 与确定性 Recall/MRR、Citation、延迟、Token 和成本合并成同一份版本化报告。</span>
              </div>
            </li>
          </ul>
        </section>
      </div>

      <section className="aw-card aw-section" aria-labelledby="evaluation-matrix-title">
        <div className="aw-card-header">
          <div>
            <span className="aw-eyebrow">Implementation matrix</span>
            <h2 id="evaluation-matrix-title">实现状态</h2>
          </div>
        </div>
        <div className="aw-evaluation-matrix" role="table" aria-label="评测实现状态">
          <div className="aw-evaluation-row is-heading" role="row">
            <span role="columnheader">能力</span>
            <span role="columnheader">当前</span>
            <span role="columnheader">基线目标</span>
          </div>
          <EvaluationRow
            capability="确定性 Retrieval 指标"
            current="已有离线 Runner；页面未接报告 API"
            target="持续保留，作为 RAGAS 的互补证据"
          />
          <EvaluationRow
            capability="LlamaIndex"
            current="选型已确定，后端 Adapter 待接入"
            target="负责 ingestion 与 retrieval 边界"
          />
          <EvaluationRow
            capability="RAGAS"
            current="尚未接入，不报告分数"
            target="Resume v1 必须完成的回答质量基线"
          />
          <EvaluationRow
            capability="可展示报告"
            current="无公开查询接口"
            target="版本化、可追溯、可重复运行"
          />
        </div>
      </section>
    </main>
  );
}

function EvaluationRow({
  capability,
  current,
  target,
}: {
  capability: string;
  current: string;
  target: string;
}) {
  return (
    <div className="aw-evaluation-row" role="row">
      <strong role="cell">{capability}</strong>
      <span role="cell">{current}</span>
      <span role="cell">{target}</span>
    </div>
  );
}
