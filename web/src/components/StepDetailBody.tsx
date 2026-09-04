import { FileDown } from "lucide-react";
import type { ArtifactRef } from "../api/types";
import { JsonView } from "./JsonView";
import type { StepBody, StepFact } from "./stepDetail";

/**
 * 一步展开之后的正文：几行事实、几段正文、也许一个产物。
 *
 * 从 `StepDisclosure` 里抽出来，因为现在有两个地方要画同一样东西：一条事件展开
 * 之后（Task 与 Chat 的时间线），和一次调用展开之后（Code 的动作行、Task 时间线
 * 上折成一行的那个组）。两处各画一遍的下场是「JSON 在这边是两列、在那边是一行
 * 转义」——而那正是这次要修的东西。
 *
 * 正文是 JSON 时交给 `JsonView` 按形状画，不是 JSON 时按文本画。`StepBody.value`
 * 由 `stepDetail` 在解析成功时给出，这里不再解析第二次。
 */
export function StepDetailBody({
  artifact,
  bodies,
  emptyText,
  facts,
  onOpenArtifact,
}: {
  artifact: ArtifactRef | null;
  bodies: readonly StepBody[];
  /** 三样都没有时说的那一句；不给就什么也不画。 */
  emptyText?: string;
  facts: readonly StepFact[];
  onOpenArtifact?: ((artifact: ArtifactRef) => void) | undefined;
}) {
  const hasDetail = facts.length > 0 || bodies.length > 0 || artifact !== null;
  return (
    <>
      {facts.length === 0 ? null : (
        <dl className="aw-step-facts">
          {facts.map((item) => (
            <div className={item.wide === true ? "is-wide" : ""} key={item.label}>
              <dt>{item.label}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {bodies.map((body) => (
        <figure className="aw-step-output" key={body.label}>
          <figcaption>{body.label}</figcaption>
          {body.value === undefined ? (
            <pre className={`aw-step-pre is-${body.format}`}>{body.text}</pre>
          ) : (
            <div className="aw-step-json">
              <JsonView value={body.value} />
            </div>
          )}
        </figure>
      ))}
      {artifact === null || onOpenArtifact === undefined ? null : (
        <div className="aw-step-artifact">
          <FileDown aria-hidden="true" size={15} />
          <span>
            <strong>{artifact.filename ?? artifact.kind}</strong>
            <small>
              {artifact.media_type} · {artifact.size_bytes} 字节
            </small>
          </span>
          <button
            className="aw-button is-ghost is-small"
            onClick={() => {
              onOpenArtifact(artifact);
            }}
            type="button"
          >
            打开产物
          </button>
        </div>
      )}
      {hasDetail || emptyText === undefined ? null : (
        <p className="aw-muted">{emptyText}</p>
      )}
    </>
  );
}
