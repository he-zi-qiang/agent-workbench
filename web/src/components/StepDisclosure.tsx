import { ChevronRight } from "lucide-react";
import type { ArtifactRef, EventEnvelope } from "../api/types";
import { StepDetailBody } from "./StepDetailBody";
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
  bodies = true,
  event,
  title,
  onOpenArtifact,
}: {
  /**
   * 带不带正文（提示词、思考摘要、参数、返回）。
   *
   * Code 的「事件记录」不带：那一折是审计用的原料，而它旁边的转录已经把思考画
   * 在它促成的动作上面、把返回画在动作展开之后——同一段话在同一轮里再出现一次，
   * 正是 ADR-064 那条「推理只渲染一次」要挡的东西。事实与原始载荷照旧。
   */
  bodies?: boolean;
  event: EventEnvelope;
  title: string;
  onOpenArtifact?: (artifact: ArtifactRef) => void;
}) {
  const detail = describeEvent(event);

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
        <StepDetailBody
          artifact={detail.artifact}
          bodies={bodies ? detail.bodies : []}
          emptyText="这个事件没有额外内容，只记录它发生过。"
          facts={detail.facts}
          onOpenArtifact={onOpenArtifact}
        />
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
