import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import type { ArtifactRef, EventEnvelope } from "../api/types";
import { StepDisclosure } from "./StepDisclosure";
import {
  groupSteps,
  summariseGroups,
  type GateStep,
  type StepGroup,
  type StepOutcome,
} from "./stepGroups";

/**
 * A run, as the stages it went through, each openable to its real steps.
 *
 * Shared between Work and Chat because the reader's question is the same in
 * both -- what did it do, and what actually went in and out of each step --
 * and answering it two different ways taught the same thing twice.
 *
 * What is *not* shared is where the stages come from. Work has a graph and
 * groups by node; a Chat turn has no `graph_node_id` at all and groups by what
 * its events mean. Those are genuinely different readings of different data,
 * so each caller derives its own stages and this renders them.
 */

export type StreamStageState =
  | "done"
  | "active"
  | "failed"
  | "waiting"
  | "pending"
  | "skipped";

export interface StreamStage {
  id: string;
  title: string;
  state: StreamStageState;
  /** Right-aligned text: a time once finished, a status while not. */
  note: string;
  /**
   * The graph nodes behind the title, when the caller has a graph.
   *
   * Absent for Chat, which has none -- its stages are derived from what the
   * events mean, so there is no node id to print and a made-up one would be
   * worse than the blank.
   */
  nodes?: string;
  /** How long the stage took, once it is over. */
  duration?: string;
  events: EventEnvelope[];
}

export interface StreamMeta {
  title: string;
  events: EventEnvelope[];
}

/**
 * Success is the unmarked case. A step that worked says nothing on its line --
 * the reader is scanning for the one that did not, and a column of green ticks
 * is what makes a single failure hard to find.
 */
const OUTCOME_LABELS: Readonly<Record<StepOutcome, string>> = {
  ok: "",
  failed: "失败",
  denied: "被拒绝",
  running: "进行中",
};

/**
 * A tool call's authorization sequence, on the collapsed line.
 *
 * On the line rather than inside the disclosure because the whole claim is
 * that the *order* is the argument: proposed, authorized, started, finished,
 * and a call that stopped shows which of the four it stopped at. Folded away,
 * it would only be found by a reader who already suspected something.
 *
 * Deliberately quieter than the design sketch, which paints a completed bead
 * green. Four green pills per row, on a stage that read twelve pages, is the
 * column of ticks `OUTCOME_LABELS` exists to avoid -- it makes the one denied
 * call harder to find, not easier. So a bead that went through is neutral and
 * only 被拒 / 失败 carry colour: this is a progress track, not a status column.
 */
function GateBeads({ steps }: { steps: GateStep[] | null }) {
  if (steps === null) return null;

  return (
    <span className="aw-step-gate">
      {steps.map((step) => (
        <span className={`aw-step-bead is-${step.state}`} key={step.key}>
          <span aria-hidden="true" className="aw-step-bead-dot" />
          {step.label}
        </span>
      ))}
    </span>
  );
}

export function StepStream({
  ariaLabel,
  eventTitle,
  isKnownEvent,
  legend,
  meta,
  onOpenArtifact,
  running,
  stages,
}: {
  ariaLabel: string;
  eventTitle: (event: EventEnvelope) => string;
  /** Unknown event types stay visible, dimmed, rather than being dropped. */
  isKnownEvent?: (event: EventEnvelope) => boolean;
  /**
   * A key for the dots, drawn above the first stage.
   *
   * Optional because only one caller has anything to key. Work renders a
   * *declared* list -- stages that have not run are on screen from the first
   * frame -- so the column of dots carries states the events themselves never
   * mention. A Chat turn's stages are derived from events that did happen, so
   * there is no unreached dot there and a key would explain a symbol the page
   * never draws.
   */
  legend?: ReactNode;
  /** Run- or task-level bookkeeping, folded away under the stages. */
  meta?: StreamMeta;
  onOpenArtifact?: (artifact: ArtifactRef) => void;
  running: boolean;
  stages: StreamStage[];
}) {
  const step = (event: EventEnvelope) => (
    <li
      className={
        isKnownEvent === undefined || isKnownEvent(event) ? "" : "is-unknown"
      }
      key={event.event_id}
    >
      <StepDisclosure
        event={event}
        title={eventTitle(event)}
        {...(onOpenArtifact === undefined ? {} : { onOpenArtifact })}
      />
    </li>
  );

  /**
   * One step as the reader names it, opening to the events it was folded from.
   *
   * A group holding a single event is rendered exactly as it always was: there
   * is nothing to fold, and wrapping it would put a second caret in front of a
   * row that already has one.
   */
  const groupStep = (group: StepGroup) => {
    const only = group.events.length === 1 ? group.events[0] : undefined;
    if (only !== undefined) return step(only);

    return (
      <li key={group.key}>
        <details className={`aw-step-group is-${group.outcome}`}>
          <summary className="aw-step-group-head">
            <ChevronRight
              aria-hidden="true"
              className="aw-step-caret"
              size={13}
            />
            <span className="aw-step-group-title">{group.title}</span>
            {group.subject === null ? null : (
              <span className="aw-step-group-subject" title={group.subject}>
                {group.subject}
              </span>
            )}
            <span className="aw-step-group-outcome">
              {OUTCOME_LABELS[group.outcome]}
            </span>
            <GateBeads steps={group.gate} />
          </summary>
          <ol className="aw-stream-events">{group.events.map(step)}</ol>
        </details>
      </li>
    );
  };

  // The same vocabulary the rows use, so a digest cannot read half-translated:
  // without this a stage summarised as "RunStarted · 模型作答 · RunCompleted".
  const groupsOf = (events: EventEnvelope[]) => groupSteps(events, eventTitle);

  return (
    <section className="aw-stream" aria-label={ariaLabel}>
      {legend}
      <ol className="aw-stream-steps">
        {stages.map((stage) => {
          const groups = groupsOf(stage.events);
          return (
          <li className={`aw-stream-state is-${stage.state}`} key={stage.id}>
            <span className="aw-stream-dot" aria-hidden="true" />
            {stage.events.length === 0 ? (
              <div className="aw-stream-head is-empty">
                <span className="aw-stream-title">{stage.title}</span>
                <span className="aw-stream-note">{stage.note}</span>
              </div>
            ) : (
              // Only the stage that is moving is open. A finished run collapses
              // to one line per stage, and opening one shows the prompts, tool
              // calls and outputs that produced it.
              <details
                className="aw-stream-step"
                open={running && stage.state === "active"}
              >
                <summary className="aw-stream-head">
                  <ChevronRight
                    aria-hidden="true"
                    className="aw-step-caret"
                    size={13}
                  />
                  <span className="aw-stream-title">{stage.title}</span>
                  {/* 图里的节点名。标题是读者的话，这是日志的话——手里拿着一条
                      trace、一份 ADR 或某个事件的 graph_node_id 的人，没有别的
                      地方能把「收集资料」和 research_internal 对上。 */}
                  {stage.nodes === undefined ? null : (
                    <code className="aw-stream-nodes">{stage.nodes}</code>
                  )}
                  {/* What it did, not how much of it. A collapsed stage is the
                      only thing on screen until somebody clicks, and "16 步"
                      spends that line on a quantity -- the count is still there
                      inside the digest, as the ×N on each kind. */}
                  <span className="aw-stream-count" title={`${groups.length} 步`}>
                    {summariseGroups(groups)}
                  </span>
                  {stage.duration === undefined ? null : (
                    <span className="aw-stream-duration">{stage.duration}</span>
                  )}
                  <span className="aw-stream-note">{stage.note}</span>
                </summary>
                <ol className="aw-stream-events">{groups.map(groupStep)}</ol>
              </details>
            )}
          </li>
          );
        })}
      </ol>
      {meta === undefined || meta.events.length === 0 ? null : (
        <details className="aw-stream-meta">
          <summary>
            <ChevronRight aria-hidden="true" className="aw-step-caret" size={13} />
            {meta.title}
            <span>{meta.events.length} 条</span>
          </summary>
          {/* Counted as events, not steps: this block is the bookkeeping, and
              a reader who opened it came for the rows themselves. */}
          <ol className="aw-stream-events">{meta.events.map(step)}</ol>
        </details>
      )}
    </section>
  );
}
