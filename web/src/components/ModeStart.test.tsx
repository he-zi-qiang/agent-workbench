import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ModeStarterPrompts,
  submitTextareaOnEnter,
} from "./ModeStart";

function KeyboardForm({ onSubmit }: { onSubmit: () => void }) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <textarea aria-label="指令" onKeyDown={submitTextareaOnEnter} />
      <button type="submit">发送</button>
    </form>
  );
}

describe("mode composer keyboard behaviour", () => {
  it("submits once on Enter", () => {
    const onSubmit = vi.fn();
    render(<KeyboardForm onSubmit={onSubmit} />);

    fireEvent.keyDown(screen.getByLabelText("指令"), { key: "Enter" });

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps Shift+Enter for a newline", () => {
    const onSubmit = vi.fn();
    render(<KeyboardForm onSubmit={onSubmit} />);

    fireEvent.keyDown(screen.getByLabelText("指令"), {
      key: "Enter",
      shiftKey: true,
    });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not submit during IME composition or a repeated keydown", () => {
    const onSubmit = vi.fn();
    render(<KeyboardForm onSubmit={onSubmit} />);
    const field = screen.getByLabelText("指令");

    fireEvent.keyDown(field, { isComposing: true, key: "Enter" });
    fireEvent.keyDown(field, { key: "Enter", keyCode: 229 });
    fireEvent.keyDown(field, { key: "Enter", repeat: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("ModeStarterPrompts", () => {
  it("exposes the suggestions as one named group", () => {
    const onChoose = vi.fn();
    render(
      <ModeStarterPrompts
        items={[{ title: "规划一个实现", prompt: "先规划" }]}
        label="编码任务起点"
        onChoose={onChoose}
      />,
    );

    expect(
      screen.getByRole("group", { name: "编码任务起点" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "规划一个实现" }));
    expect(onChoose).toHaveBeenCalledWith("先规划");
  });

  it("does not change the draft while its form is busy", () => {
    const onChoose = vi.fn();
    render(
      <ModeStarterPrompts
        disabled
        items={[{ title: "规划一个实现", prompt: "先规划" }]}
        label="编码任务起点"
        onChoose={onChoose}
      />,
    );

    const starter = screen.getByRole("button", { name: "规划一个实现" });
    expect(starter).toBeDisabled();
    fireEvent.click(starter);
    expect(onChoose).not.toHaveBeenCalled();
  });
});
