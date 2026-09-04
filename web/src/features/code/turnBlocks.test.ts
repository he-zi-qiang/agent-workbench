import { describe, expect, it } from "vitest";
import type { EventEnvelope, MessageView } from "../../api/types";
import { buildTurnBlocks } from "./turnBlocks";

/**
 * The pairing between instructions and runs is the one place in this feature
 * that can be *silently wrong* -- a card attributed to the turn above the one
 * that made it looks exactly like a card that is right. `turnStages.ts`, which
 * this replaced, had no tests at all; these exist mostly for the failure modes
 * that produce a plausible answer rather than an error.
 */

let nextEvent = 0;

function event(
  runId: string,
  type: string,
  payload: Record<string, unknown> = {},
): EventEnvelope {
  nextEvent += 1;
  return {
    schema_version: 1,
    event_id: `evt_${String(nextEvent)}`,
    stream_id: "stream_1",
    run_id: runId,
    event_type: type,
    durability: "durable",
    timestamp: "2026-08-14T12:00:00Z",
    payload: { kind: type, ...payload },
    sequence: nextEvent,
    task_id: null,
    graph_node_id: null,
    parent_event_id: null,
  };
}

/** One successful workspace_write, as the server actually emits it. */
function wrote(runId: string, name: string, callId = `call_${name}`) {
  return [
    event(runId, "ToolProposed", {
      tool_call_id: callId,
      tool_name: "workspace_write",
      argument_preview: JSON.stringify({ content: "…", name }),
    }),
    event(runId, "ToolCompleted", {
      tool_call_id: callId,
      workspace_writes: [name],
    }),
  ];
}

function said(...texts: string[]): MessageView[] {
  return texts.map((text, index) =>
    index % 2 === 0
      ? { role: "user", text }
      : { role: "assistant", text },
  );
}

describe("buildTurnBlocks", () => {
  it("pairs instructions with runs from the tail, not the head", () => {
    // Three instructions, but the stream only kept the last two runs --
    // `KEPT_EVENTS` is 2000 and a long session loses its oldest. Anchoring at
    // the head would shift every block by one and put run_b's files under the
    // second instruction.
    const { blocks, orphanRuns } = buildTurnBlocks({
      messages: said("一", "报告一", "二", "报告二", "三", "报告三"),
      events: [...wrote("run_b", "b.md"), ...wrote("run_c", "c.md")],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks.map((block) => block.instruction)).toEqual(["一", "二", "三"]);
    expect(blocks.map((block) => block.runId)).toEqual([null, "run_b", "run_c"]);
    expect(blocks[0]?.produced).toEqual([]);
    expect(blocks[1]?.produced.map((file) => file.name)).toEqual(["b.md"]);
    expect(blocks[2]?.produced.map((file) => file.name)).toEqual(["c.md"]);
    expect(orphanRuns).toBe(0);
  });

  it("drops the oldest runs rather than mis-attributing them", () => {
    // Another tab ran a turn in this same session, so the stream holds more
    // runs than this transcript has instructions. A card on the wrong turn is
    // a lie; a card that is missing is a gap the page can admit to, and the
    // panel's heading says so.
    const { blocks, orphanRuns } = buildTurnBlocks({
      messages: said("只有这一句"),
      events: [
        ...wrote("run_other", "theirs.md"),
        ...wrote("run_mine", "mine.md"),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.runId).toBe("run_mine");
    expect(blocks[0]?.produced.map((file) => file.name)).toEqual(["mine.md"]);
    expect(orphanRuns).toBe(1);
  });

  it("pairs a turn that came back without a report", () => {
    // A turn that runs out of budget appends no assistant message at all, so
    // `messages[2k]` is off by one for every turn after it.
    const { blocks } = buildTurnBlocks({
      messages: [
        { role: "user", text: "一" },
        { role: "user", text: "二" },
        { role: "assistant", text: "报告二" },
      ],
      events: [...wrote("run_a", "a.md"), ...wrote("run_b", "b.md")],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks.map((block) => block.report)).toEqual([null, "报告二"]);
    expect(blocks.map((block) => block.runId)).toEqual(["run_a", "run_b"]);
  });

  it("gives the run with no terminal record to the pending block", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一", "报告一"),
      events: [
        ...wrote("run_a", "a.md"),
        event("run_a", "RunCompleted"),
        ...wrote("run_b", "b.md"),
      ],
      running: true,
      pendingInstruction: "二",
      liveCallId: "",
    });

    expect(blocks).toHaveLength(2);
    expect(blocks[0]?.live).toBe(false);
    expect(blocks[0]?.runId).toBe("run_a");
    // The live turn's card is already there, with no report and no terminal
    // run event: "生成的文件应该在对话生成中" is a claim about timing.
    expect(blocks[1]?.live).toBe(true);
    expect(blocks[1]?.runId).toBe("run_b");
    expect(blocks[1]?.produced.map((file) => file.name)).toEqual(["b.md"]);
  });

  it("gives the live run to the last instruction once the server has it", () => {
    // The other half of the same race, and the one that used to have no
    // answer. The server appends the user message *before* the run starts, so
    // a transcript re-read that lands after that append carries the sentence
    // and the page has nothing pending left to hand a block to. Measured on a
    // real session: the instruction showed and its steps, its thinking and its
    // report did not, for the whole of the turn.
    const { blocks, orphanRuns } = buildTurnBlocks({
      messages: said("一", "报告一", "二"),
      events: [
        ...wrote("run_a", "a.md"),
        event("run_a", "RunCompleted"),
        ...wrote("run_b", "b.md"),
      ],
      running: true,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks).toHaveLength(2);
    // The settled run must not slide forward onto the live instruction: with
    // one fewer slot to pair against, run_a still belongs to 一.
    expect(blocks[0]?.live).toBe(false);
    expect(blocks[0]?.runId).toBe("run_a");
    expect(blocks[0]?.produced.map((file) => file.name)).toEqual(["a.md"]);
    expect(blocks[1]?.live).toBe(true);
    expect(blocks[1]?.runId).toBe("run_b");
    expect(blocks[1]?.produced.map((file) => file.name)).toEqual(["b.md"]);
    expect(orphanRuns).toBe(0);
  });

  it("counts orphans against the slots the settled runs actually have", () => {
    // One instruction, which the live run has claimed, and a settled run from
    // another tab. Nothing is left for it to pair with, and saying so is what
    // keeps its files off the turn a reader is watching.
    const { blocks, orphanRuns } = buildTurnBlocks({
      messages: said("只有这一句"),
      events: [
        ...wrote("run_other", "theirs.md"),
        event("run_other", "RunCompleted"),
        ...wrote("run_mine", "mine.md"),
      ],
      running: true,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.runId).toBe("run_mine");
    expect(blocks[0]?.produced.map((file) => file.name)).toEqual(["mine.md"]);
    expect(orphanRuns).toBe(1);
  });

  it("never reports more orphans than there are runs", () => {
    // One frame of switching sessions mid-turn: `running` is still true and
    // the new session's transcript has not arrived, so there is a live run and
    // no instruction to give it to. Unfloored, the settled slots would be -1
    // and the panel would print an orphan count higher than the run count.
    const { blocks, orphanRuns } = buildTurnBlocks({
      messages: [],
      events: [
        ...wrote("run_a", "a.md"),
        event("run_a", "RunCompleted"),
        ...wrote("run_b", "b.md"),
      ],
      running: true,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks).toEqual([]);
    expect(orphanRuns).toBe(1);
  });

  it("calls nothing live once the request is closed", () => {
    // A run with no terminal record that nothing is waiting on did not end --
    // the process holding it died. Drawing it as active would spin forever.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: wrote("run_a", "a.md"),
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.live).toBe(false);
    expect(blocks[0]?.runId).toBe("run_a");
  });

  it("does not card a write that was denied or failed", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ToolProposed", {
          tool_call_id: "call_1",
          tool_name: "workspace_write",
          argument_preview: JSON.stringify({ name: "denied.md" }),
        }),
        event("run_a", "PermissionResolved", {
          tool_call_id: "call_1",
          effect: "deny",
        }),
        event("run_a", "ToolFailed", { tool_call_id: "call_1" }),
        event("run_a", "ToolProposed", {
          tool_call_id: "call_2",
          tool_name: "workspace_write",
          argument_preview: JSON.stringify({ name: "failed.md" }),
        }),
        event("run_a", "ToolFailed", { tool_call_id: "call_2" }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.produced).toEqual([]);
    // The attempts are still in the action list, marked -- the card is about
    // what exists, the row is about what happened.
    expect(blocks[0]?.groups.map((group) => group.outcome)).toEqual([
      "denied",
      "failed",
    ]);
  });

  it("says which later turn rewrote a file, and never claims a file is new", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一", "报告一", "二", "报告二"),
      events: [
        ...wrote("run_a", "notes.md", "call_a"),
        ...wrote("run_b", "notes.md", "call_b"),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    const first = blocks[0]?.produced[0];
    const second = blocks[1]?.produced[0];
    // Turn 1 wrote it first *as far as this stream saw*, so it does not claim
    // to have created it -- the workspace has a history older than the window.
    expect(first?.overwrote).toBe(false);
    expect(first?.supersededByTurn).toBe(2);
    // Turn 2 overwrote something this stream watched being written, which is
    // the only case where "覆盖" is a fact rather than a guess.
    expect(second?.overwrote).toBe(true);
    expect(second?.supersededByTurn).toBeNull();
  });

  it("falls back to the argument preview, and stays quiet when neither has a name", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一", "报告一", "二", "报告二", "三", "报告三"),
      events: [
        // Pre-ADR-063: no workspace_writes, name still inside the 4KB preview.
        event("run_a", "ToolProposed", {
          tool_call_id: "call_1",
          tool_name: "workspace_write",
          argument_preview: JSON.stringify({ content: "x", name: "old.md" }),
        }),
        event("run_a", "ToolCompleted", { tool_call_id: "call_1" }),
        // The case ADR-063 exists for: a body over BOUNDED_TEXT_LIMIT, so the
        // canonical JSON is cut before `name` (it sorts after `content`) and
        // arrives unparseable. No card, and no crash.
        event("run_b", "ToolProposed", {
          tool_call_id: "call_2",
          tool_name: "workspace_write",
          argument_preview: '{"content": "aaaaaaaaaaaaaaaaaaaa',
        }),
        event("run_b", "ToolCompleted", { tool_call_id: "call_2" }),
        // A tool with no name field at all is not mined for one.
        event("run_c", "ToolProposed", {
          tool_call_id: "call_3",
          tool_name: "workspace_grep",
          argument_preview: JSON.stringify({ query: "name" }),
        }),
        event("run_c", "ToolCompleted", { tool_call_id: "call_3" }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.produced.map((file) => file.name)).toEqual(["old.md"]);
    expect(blocks[1]?.produced).toEqual([]);
    expect(blocks[2]?.produced).toEqual([]);
  });

  it("names the verb after what the call did, not after what the file is", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ToolProposed", {
          tool_call_id: "call_1",
          tool_name: "workspace_edit",
        }),
        event("run_a", "ToolCompleted", {
          tool_call_id: "call_1",
          workspace_writes: ["edited.md"],
        }),
        event("run_a", "ToolProposed", {
          tool_call_id: "call_2",
          tool_name: "sandbox_run",
        }),
        event("run_a", "ToolCompleted", {
          tool_call_id: "call_2",
          workspace_writes: ["out.csv"],
        }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.produced.map((file) => [file.name, file.action])).toEqual([
      ["edited.md", "edit"],
      ["out.csv", "run"],
    ]);
  });

  it("keeps run and model bookkeeping out of the action list but not out of the record", () => {
    const events = [
      event("run_a", "RunStarted"),
      event("run_a", "ModelStarted", { model_call_id: "mc_1" }),
      event("run_a", "ModelCompleted", {
        model_call_id: "mc_1",
        text: "",
        tool_call_ids: ["call_notes.md"],
        thinking_preview: "先写文件",
      }),
      ...wrote("run_a", "notes.md"),
      event("run_a", "RunCompleted"),
    ];
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events,
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    // One action offered, not six protocol rows.
    expect(blocks[0]?.groups).toHaveLength(1);
    expect(blocks[0]?.groups[0]?.title).toBe("写入工作区");
    // The builder retains every durable event for grouping and provenance, so
    // the quiet diagnostic disclosure can show them without making protocol
    // rows the default transcript.
    expect(blocks[0]?.events).toHaveLength(events.length);
    // And the thought is ON that action, not in a list of its own. This is the
    // whole of what `groupSteps` already knew and this module used to discard.
    expect(blocks[0]?.steps).toHaveLength(1);
    expect(blocks[0]?.steps[0]?.key).toBe("tool:call_notes.md");
    expect(blocks[0]?.steps[0]?.modelCallId).toBe("mc_1");
    expect(blocks[0]?.steps[0]?.thinking).toBe("先写文件");
    expect(blocks[0]?.steps[0]?.group?.title).toBe("写入工作区");
  });

  it("puts each model call's thought on the step it caused", () => {
    // A turn is think → call a tool → think again → call another. The old
    // shape proved only that the two thoughts were not joined into one
    // paragraph; what it could not prove -- and what the reader actually needs
    // -- is that each one sits on the command it explains.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ModelCompleted", {
          model_call_id: "mc_1",
          text: "",
          tool_call_ids: ["call_a.md"],
          thinking_preview: "先建文件",
        }),
        ...wrote("run_a", "a.md", "call_a.md"),
        event("run_a", "ModelCompleted", {
          model_call_id: "mc_2",
          text: "",
          tool_call_ids: ["call_b.md"],
          thinking_preview: "再读回来核对",
        }),
        ...wrote("run_a", "b.md", "call_b.md"),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(
      blocks[0]?.steps.map((step) => [step.thinking, step.group?.title ?? null]),
    ).toEqual([
      ["先建文件", "写入工作区"],
      ["再读回来核对", "写入工作区"],
    ]);
  });

  it("pairs the thought with its call even when the model narrated", () => {
    // Measured on a real session: DeepSeek says something *and* calls a tool on
    // some turns and not others -- three of four calls in one run. `groupSteps`
    // only folds the silent ones, so relying on that alone put the thought and
    // the command it explains on two sibling rows for the talkative ones.
    // The join is done here from `tool_call_ids` instead, so pairing does not
    // depend on whether the model felt like narrating.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ModelCompleted", {
          model_call_id: "mc_1",
          text: "The workspace is empty, writing the file now.",
          tool_call_ids: ["call_a.md"],
          thinking_preview: "工作区是空的，直接建文件",
        }),
        ...wrote("run_a", "a.md", "call_a.md"),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.steps).toHaveLength(1);
    expect(blocks[0]?.steps[0]?.thinking).toBe("工作区是空的，直接建文件");
    expect(blocks[0]?.steps[0]?.group?.title).toBe("写入工作区");
    expect(blocks[0]?.steps[0]?.modelCallId).toBe("mc_1");
  });

  it("keeps the answering turn's thought where the report follows it", () => {
    // The turn that said something rather than calling something. It has no
    // action, and filtering the timeline to `tool:` groups would drop its
    // reasoning entirely -- which is exactly the last thought of every turn.
    const { blocks } = buildTurnBlocks({
      messages: said("一", "报告"),
      events: [
        event("run_a", "ModelCompleted", {
          model_call_id: "mc_1",
          text: "写好了。",
          thinking_preview: "交代一下做了什么",
        }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.steps).toHaveLength(1);
    expect(blocks[0]?.steps[0]?.thinking).toBe("交代一下做了什么");
    expect(blocks[0]?.steps[0]?.group).toBeNull();
  });

  it("leaves an anchor for the call that has not come back", () => {
    // `ModelStarted` is durable and arrives before the first thinking delta.
    // The step it creates is where the live text lands, so that the thought a
    // reader is watching is already in the position it will settle into.
    // The live run's events belong to the pending block, so the instruction
    // has to be in flight for there to be a block holding them at all.
    const { blocks } = buildTurnBlocks({
      messages: [],
      events: [event("run_a", "ModelStarted", { model_call_id: "mc_1" })],
      running: true,
      pendingInstruction: "写个时钟",
      liveCallId: "mc_1",
    });

    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.live).toBe(true);
    expect(blocks[0]?.steps).toHaveLength(1);
    expect(blocks[0]?.steps[0]?.modelCallId).toBe("mc_1");
    expect(blocks[0]?.steps[0]?.thinking).toBe("");
    expect(blocks[0]?.steps[0]?.group).toBeNull();
  });

  it("leaves no empty row for a call that will never come back", () => {
    // A settled block with a ModelStarted and no ModelCompleted: the page was
    // reloaded mid-turn, or the process holding the run died. The anchor has
    // nothing left to receive, and measured on a real session it rendered a
    // zero-height `<li>` that still took the step list's gap.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [event("run_a", "ModelStarted", { model_call_id: "mc_1" })],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.steps).toEqual([]);
  });

  it("never files one model call as two steps", () => {
    // A turn naming two calls produces two action rows either way; the thought
    // goes on the earlier one. Rendering it on both would show the same
    // reasoning twice, which is the defect this whole area exists to prevent.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ModelCompleted", {
          model_call_id: "mc_1",
          text: "",
          tool_call_ids: ["call_a.md", "call_b.md"],
          thinking_preview: "两件事一起做",
        }),
        ...wrote("run_a", "a.md", "call_a.md"),
        ...wrote("run_a", "b.md", "call_b.md"),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    const carrying = blocks[0]?.steps.filter((step) => step.thinking !== "");
    expect(carrying).toHaveLength(1);
    expect(carrying?.[0]?.key).toBe("tool:call_a.md");
  });

  it("drops a model turn that neither thought nor was left hanging", () => {
    // No thought, already returned, produced text handled elsewhere: there is
    // nothing to draw, and an empty row in the timeline reads as a step that
    // did something unnameable.
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "ModelCompleted", { model_call_id: "mc_1", text: "hi" }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    expect(blocks[0]?.steps).toEqual([]);
  });
});


describe("buildTurnBlocks：没跑完的那一轮为什么停", () => {
  it("失败的运行带着错误码和服务端那句话", () => {
    const events = [
      event("run_a", "RunStarted"),
      event("run_a", "RunFailed", {
        stop_reason: "error",
        error: {
          code: "provider_error",
          message: "the provider rejected the request with HTTP 400",
          retryable: false,
        },
      }),
    ];
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events,
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });

    // 此前这一轮只有指令和一行 0 → 0 的脚注，什么也没说。
    expect(blocks[0]?.stop).toEqual({
      kind: "failed",
      reason: "error",
      code: "provider_error",
      message: "the provider rejected the request with HTTP 400",
    });
  });

  it("撞了上限的运行是 ceiling，正常完成的是 null", () => {
    const stopped = buildTurnBlocks({
      messages: said("一"),
      events: [
        event("run_a", "RunStarted"),
        event("run_a", "RunCompleted", { stop_reason: "deadline" }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });
    expect(stopped.blocks[0]?.stop).toEqual({
      kind: "ceiling",
      reason: "deadline",
      code: null,
      message: null,
    });

    const fine = buildTurnBlocks({
      messages: said("一", "报告"),
      events: [
        event("run_a", "RunStarted"),
        event("run_a", "RunCompleted", { stop_reason: "completed" }),
      ],
      running: false,
      pendingInstruction: null,
      liveCallId: "",
    });
    // 成功靠不说话来说。
    expect(fine.blocks[0]?.stop).toBeNull();
  });

  it("还在跑的那一轮没有终局，也就没有这一行", () => {
    const { blocks } = buildTurnBlocks({
      messages: said("一"),
      events: [event("run_a", "RunStarted")],
      running: true,
      pendingInstruction: "二",
      liveCallId: "",
    });
    expect(blocks.map((block) => block.stop)).toEqual([null, null]);
  });
});
