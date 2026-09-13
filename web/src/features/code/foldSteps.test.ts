import { describe, expect, it } from "vitest";
import type { StepGroup, StepOutcome } from "../../components/stepGroups";
import { FOLD_MIN, VISIBLE_TAIL, foldHead, foldSteps } from "./foldSteps";
import type { TurnStep } from "./turnBlocks";

function step(
  key: string,
  title: string,
  outcome: StepOutcome = "ok",
  thinking = "",
): TurnStep {
  const group: StepGroup = {
    key,
    title,
    subject: null,
    outcome,
    gate: null,
    events: [],
  };
  return { key, modelCallId: `mc_${key}`, thinking, group };
}

function commands(count: number, outcome: StepOutcome = "ok"): TurnStep[] {
  return Array.from({ length: count }, (_, index) =>
    step(`tool:call_${String(index)}`, "在本机执行命令", outcome),
  );
}

describe("foldSteps（ADR-0121）", () => {
  it("113 步一模一样的命令折成一行，前后不同的动作各自一行", () => {
    // 用户贴回来的那一轮的形状。
    const steps = [
      step("tool:list", "查看项目目录"),
      step("tool:read", "读取项目目录", "failed"),
      ...commands(113),
      step("model:report", "回答"),
    ];

    const folded = foldSteps(steps);

    expect(folded.map((item) => item.kind)).toEqual(["step", "step", "fold", "step"]);
    const fold = folded[2];
    expect(fold?.kind === "fold" && fold.steps.length).toBe(113);
    expect(fold?.kind === "fold" && fold.title).toBe("在本机执行命令");
    // 由第一步派生：多一步不会换一个 key。
    expect(fold?.kind === "fold" && fold.key).toBe("fold:tool:call_0");
  });

  it("不到三步不折", () => {
    const folded = foldSteps(commands(FOLD_MIN - 1));

    expect(folded.every((item) => item.kind === "step")).toBe(true);
  });

  it("有自己的话的一步不折，而且把前后分成两串", () => {
    const steps = [
      ...commands(3),
      step("tool:say", "在本机执行命令", "ok", "先量一下每一行的长度"),
      ...commands(3).map((one, index) => ({ ...one, key: `tool:later_${String(index)}` })),
    ];

    const folded = foldSteps(steps);

    expect(folded.map((item) => item.kind)).toEqual(["fold", "step", "fold"]);
  });

  it("失败的和成功的分开折：失败不藏进成功的数字里", () => {
    const steps = [...commands(4), ...commands(3, "failed").map((one, index) => ({
      ...one,
      key: `tool:bad_${String(index)}`,
    }))];

    const folded = foldSteps(steps);

    expect(folded).toHaveLength(2);
    expect(folded[0]?.kind === "fold" && folded[0].outcome).toBe("ok");
    expect(folded[1]?.kind === "fold" && folded[1].outcome).toBe("failed");
  });

  it("最后一步还在跑时，这一串就是进行中", () => {
    const steps = [...commands(5), step("tool:live", "在本机执行命令", "running")];

    const folded = foldSteps(steps);

    expect(folded).toHaveLength(1);
    expect(folded[0]?.kind === "fold" && folded[0].outcome).toBe("running");
    expect(folded[0]?.kind === "fold" && folded[0].steps).toHaveLength(6);
  });

  it("正在往外流思考的那一步不折，思考要看得见", () => {
    const steps = [...commands(4), step("tool:thinking", "在本机执行命令", "running")];

    const folded = foldSteps(steps, { liveCallId: "mc_tool:thinking" });

    expect(folded.map((item) => item.kind)).toEqual(["fold", "step"]);
  });

  it("没有动作的一步（只有思考、或者正在答）从不进折叠", () => {
    const thought: TurnStep = {
      key: "model:mc_think",
      modelCallId: "mc_think",
      thinking: "",
      group: null,
    };

    const folded = foldSteps([...commands(3), thought, ...commands(2)]);

    expect(folded.map((item) => item.kind)).toEqual(["fold", "step", "step", "step"]);
  });
});

/** 交替做好几种事的一轮：编辑、求值、打开页面……没有哪一种连着三次。 */
function varied(count: number): TurnStep[] {
  const titles = ["修改项目目录文件", "在页面里求值", "打开页面", "在本机执行命令"];
  return Array.from({ length: count }, (_, index) =>
    step(`tool:varied_${String(index)}`, titles[index % titles.length] ?? "x"),
  );
}

describe("foldHead（ADR-0121 第二层）", () => {
  it("短的一轮原样不动", () => {
    const items = foldSteps(varied(VISIBLE_TAIL + 2));

    expect(foldHead(items)).toEqual(items);
  });

  it("折完还长的一轮，前面收成一行，最后几行留在眼前", () => {
    // 那一轮成功重写马里奥的回合：101 次调用，折完同一动作之后还有 59 行。
    const items = foldSteps(varied(40));

    const folded = foldHead(items);

    expect(folded).toHaveLength(VISIBLE_TAIL + 1);
    const head = folded[0];
    expect(head?.kind).toBe("head");
    if (head?.kind !== "head") return;
    expect(head.stepCount).toBe(40 - VISIBLE_TAIL);
    expect(head.summary).toContain("修改项目目录文件 ×");
    // 留在眼前的就是最后那几步。
    expect(folded.slice(1).map((item) => item.kind === "step" && item.step.key)).toEqual(
      varied(40)
        .slice(-VISIBLE_TAIL)
        .map((one) => one.key),
    );
  });

  it("一轮往下长的时候，这一行的 key 不变", () => {
    const before = foldHead(foldSteps(varied(20)));
    const after = foldHead(foldSteps(varied(21)));

    expect(before[0]?.kind === "head" && before[0].key).toBe(
      after[0]?.kind === "head" && after[0].key,
    );
  });

  it("失败的种类排在摘要最前", () => {
    const steps = [
      ...varied(12),
      step("tool:broken", "读取项目目录", "failed"),
      ...varied(8).map((one, index) => ({ ...one, key: `tool:more_${String(index)}` })),
    ];

    const folded = foldHead(foldSteps(steps));

    expect(folded[0]?.kind === "head" && folded[0].summary.startsWith("读取项目目录")).toBe(true);
  });
});
