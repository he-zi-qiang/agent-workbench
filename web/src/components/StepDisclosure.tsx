import { ChevronRight, FileDown } from "lucide-react";
import type { ArtifactRef, EventEnvelope } from "../api/types";
import { describeEvent } from "./stepDetail";
import { formatTime, shortId } from "./ui";

/**
 * One run step, openable. Shared by Work and Chat because a step means the same
 * thing on both: the model was given something, it said something, a tool was
 * proposed and allowed or refused. Collapsed it reads as a sentence; opened it
 * shows what actually happened, with the raw payload one level further down.
 *
 * The raw JSON stays reachable on purpose. A curated view is the readable
 * answer, not the authoritative one, and removing the payload would leave no
 * way to check what the curation dropped.
 */
export function StepDisclosure({
  event,
  title,
  onOpenArtifact,
}: {
  event: EventEnvelope;
  title: string;
  onOpenArtifact?: (artifact: ArtifactRef) => void;
}) {
  const detail = describeEvent(event);
  const hasDetail =
    detail.facts.length > 0 || detail.bodies.length > 0 || detail.artifact !== null;

  return (
    <details className="aw-step">
      <summary>
        <ChevronRight aria-hidden="true" className="aw-step-caret" size={14} />
        <strong>{title}</strong>
        {detail.summary === null ? null : (
          <span className="aw-step-summary">{detail.summary}</span>
        )}
        <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
      </summary>
      <div className="aw-step-body">
        {detail.facts.length === 0 ? null : (
          <dl className="aw-step-facts">
            {detail.facts.map((item) => (
              <div className={item.wide === true ? "is-wide" : ""} key={item.label}>
                <dt>{item.label}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
          </dl>
        )}
        {detail.bodies.map((body) => (
          <figure className="aw-step-output" key={body.label}>
            <figcaption>{body.label}</figcaption>
            <pre className={`aw-step-pre is-${body.format}`}>{body.text}</pre>
          </figure>
        ))}
        {detail.artifact === null || onOpenArtifact === undefined ? null : (
          <div className="aw-step-artifact">
            <FileDown aria-hidden="true" size={15} />
            <span>
              <strong>{detail.artifact.filename ?? detail.artifact.kind}</strong>
              <small>
                {detail.artifact.media_type} · {detail.artifact.size_bytes} 字节
              </small>
            </span>
            <button
              className="aw-button is-ghost is-small"
              onClick={() => {
                if (detail.artifact !== null) onOpenArtifact(detail.artifact);
              }}
              type="button"
            >
              打开产物
            </button>
          </div>
        )}
        {hasDetail ? null : (
          <p className="aw-muted">这个事件没有额外内容，只记录它发生过。</p>
        )}
        <details className="aw-step-raw">
          <summary>原始事件</summary>
          <div className="aw-timeline-event-meta">
            <code>{event.event_type}</code>
            <span title={event.run_id}>run {shortId(event.run_id)}</span>
            {event.sequence === null ? null : <span>#{event.sequence}</span>}
          </div>
          <pre>{JSON.stringify(event.payload, null, 2)}</pre>
        </details>
      </div>
    </details>
  );
}
