import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StepGroup, StepOutcome } from "../../components/stepGroups";
import { CodeTurn } from "./CodeTurn";
import type { CodeTurnBlock, TurnStep } from "./turnBlocks";

function commandStep(index: number, outcome: StepOutcome = "ok"): TurnStep {
  const key = `tool:call_${String(index)}`;
  const group: StepGroup = {
    key,
    title: "在本机执行命令",
    subject: `python3 -c "print(s[${String(index * 40)}:${String(index * 40 + 40)}])"`,
    outcome,
    gate: null,
    events: [],
  };
  return { key, modelCallId: `mc_${String(index)}`, thinking: "", group };
}

function block(steps: TurnStep[], live = false): CodeTurnBlock {
  return {
    key: "turn_1",
    index: 1,
    runId: "run_1",
    instruction: "请你重新编写马里奥",
    report: null,
    usage: null,
    stop: null,
    groups: [],
    steps,
    produced: [],
    events: [],
    live,
  };
}

function renderTurn(steps: TurnStep[], live = false) {
  return render(
    <ol>
      <CodeTurn
        block={block(steps, live)}
        files={[]}
        liveAnswer=""
        liveThinking=""
        liveThinkingCallId=""
        onOpen={() => undefined}
        openedName={null}
        toolProgress={new Map()}
      />
    </ol>,
  );
}

describe("CodeTurn 把连着的同一件事折成一行（ADR-0121）", () => {
  it("113 步一样的命令是一行「在本机执行命令 ×113」，点开才是逐条", () => {
    // 用户的原话：「如果就是有这么长的很影响感官，需要折叠和优化流式输出」。
    renderTurn(Array.from({ length: 113 }, (_, index) => commandStep(index)));
    const list = screen.getByRole("list", { name: "这一轮做了什么" });

    expect(within(list).getAllByText("在本机执行命令")).toHaveLength(1);
    expect(within(list).getByText("×113")).toBeInTheDocument();
    // 收着时逐条的那一串不在文档里。
    expect(screen.queryByRole("list", { name: /共 113 次/ })).not.toBeInTheDocument();

    fireEvent.click(within(list).getByText("×113"));

    const expanded = screen.getByRole("list", { name: "在本机执行命令，共 113 次" });
    expect(within(expanded).getAllByText("在本机执行命令")).toHaveLength(113);
  });

  it("还在跑的时候，收着的那一行底下看得见正在执行的那一步", () => {
    const steps = [
      ...Array.from({ length: 5 }, (_, index) => commandStep(index)),
      commandStep(5, "running"),
    ];

    renderTurn(steps, true);

    expect(screen.getByText("×6")).toBeInTheDocument();
    const live = screen.getByRole("list", { name: "正在进行的这一步" });
    expect(within(live).getByText("进行中")).toBeInTheDocument();
  });
});

describe("CodeTurn 把太长的一轮前面收成一行（ADR-0121 第二层）", () => {
  it("交替做好几件事的 30 步：一行「前面 24 步」加最后 6 行，点开是前面那些", () => {
    const titles = ["修改项目目录文件", "在页面里求值", "打开页面"];
    const steps = Array.from({ length: 30 }, (_, index): TurnStep => {
      const key = `tool:mixed_${String(index)}`;
      return {
        key,
        modelCallId: `mc_mixed_${String(index)}`,
        thinking: "",
        group: {
          key,
          title: titles[index % titles.length] ?? "x",
          subject: null,
          outcome: "ok",
          gate: null,
          events: [],
        },
      };
    });

    renderTurn(steps);
    const list = screen.getByRole("list", { name: "这一轮做了什么" });

    expect(list.children).toHaveLength(7);
    expect(within(list).getByText("前面 24 步")).toBeInTheDocument();

    fireEvent.click(within(list).getByText("前面 24 步"));

    const earlier = screen.getByRole("list", { name: "前面 24 步" });
    expect(earlier.children).toHaveLength(24);
  });
});
