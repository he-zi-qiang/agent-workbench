/**
 * Telling a sub-agent's events apart from its parent's, in a stream that holds
 * both.
 *
 * A Task's timeline used to be one run's worth of events, and reading it top to
 * bottom was the whole story. Delegation (ADR-082) breaks that: a delegated run
 * writes into its parent's stream under its own `run_id`, and a flat render
 * interleaves two agents' model calls and tool calls with nothing saying which
 * is which. The events are all correct and the page is unreadable.
 *
 * This works off the events the page **already holds**. The server can also
 * answer `/v1/tasks/{id}/runs` and `/timeline?run_id=`, and those exist for a
 * client that does *not* have the whole stream -- a deep link into one
 * sub-agent. Asking for them in order to render a timeline that is already in
 * memory would be a second request to learn what the first one carried.
 *
 * **Labels rather than collapsing -- and then collapsing.** This module used to
 * also export `titleWithDelegation`, which prefixed every delegated row with
 * `子代理 analyst：`, and said so in as many words: "Grouping the stream into
 * foldable sub-agent sections would be better, and it means changing the shared
 * step component every stage renders through."
 *
 * That has now happened. `components/runSections.ts` splits a stage's events
 * into contiguous runs and `StepStream` renders a foreign one as a foldable
 * block, so a sub-agent's work is one row that opens rather than nine rows
 * wearing the same prefix. The prefix is gone with it.
 *
 * **What did not move is this file's own claim**, and it is the reason the
 * module stayed: only Work can turn a `run_id` into a name, because only Work
 * has `AgentDelegated` to read. `StepStream` does the splitting, which is
 * mechanical; `readDelegations` still does the naming, which is not. A page
 * that does not hold the delegation still says so by leaving the run out --
 * and the block is drawn anyway, under `运行 xxxxxxxx`. That fallback is the
 * one thing the prefix did that a block had to keep doing: rendering a
 * stranger's events as the parent's is the single wrong answer available here,
 * and the old prefix degraded to exactly that when the page had a hole in it.
 */

import type { EventEnvelope } from "../../api/types";

/** What a page can learn about one delegated run from the delegation itself. */
export interface DelegationFacts {
  /** The sub-agent it was started as, or `null` when the payload named none. */
  definitionName: string | null;
  /** The run that started it. */
  parentRunId: string;
}

/**
 * Read every delegation this page announced, keyed by the child's run id.
 *
 * `AgentDelegated` is the only event that knows the relationship: a child's own
 * events carry its run id and nothing about who sent it. A page that does not
 * contain the delegation therefore cannot name the child's parent, and this
 * says so by leaving it out -- rather than guessing that whichever run came
 * before it was the parent.
 */
export function readDelegations(
  events: readonly EventEnvelope[],
): Map<string, DelegationFacts> {
  const facts = new Map<string, DelegationFacts>();
  for (const event of events) {
    if (event.event_type !== "AgentDelegated") continue;
    const payload = event.payload as {
      child_agent_run_id?: unknown;
      profile_name?: unknown;
    };
    const childRunId = payload.child_agent_run_id;
    if (typeof childRunId !== "string" || childRunId.length === 0) continue;
    facts.set(childRunId, {
      definitionName:
        typeof payload.profile_name === "string" && payload.profile_name !== ""
          ? payload.profile_name
          : null,
      parentRunId: event.run_id,
    });
  }
  return facts;
}
