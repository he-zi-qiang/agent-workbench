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
 * **Labels rather than collapsing.** Grouping the stream into foldable
 * sub-agent sections would be better, and it means changing the shared step
 * component every stage renders through. A name on the row is what makes the
 * events legible without that, and it is the whole of what this module claims.
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

/**
 * The title one timeline row shows, attributed to the sub-agent that produced
 * it when it was not the Task's own run.
 *
 * Prefixed rather than replaced: what happened is still what the base title
 * says, and a reader scanning for "工具调用失败" has to find it whichever run
 * it came from.
 *
 * A delegated run whose delegation named no sub-agent still gets a prefix. Its
 * events are somebody else's work either way, and rendering them as the
 * parent's would be the one wrong answer available here.
 */
export function titleWithDelegation(
  baseTitle: string,
  event: EventEnvelope,
  delegations: ReadonlyMap<string, DelegationFacts>,
): string {
  const facts = delegations.get(event.run_id);
  if (facts === undefined) return baseTitle;
  return `子代理${facts.definitionName === null ? "" : ` ${facts.definitionName}`}：${baseTitle}`;
}
