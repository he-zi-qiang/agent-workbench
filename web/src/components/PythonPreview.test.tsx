import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getDefaultNormalizer, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { RunFileResponse } from "../api/types";
import { containerLimitNote, PythonPreview, remedyFor } from "./PythonPreview";

/**
 * What this viewer must not do is as pinned as what it must.
 *
 * Running costs a container start on the server, so nothing here may start one
 * the reader did not ask for -- which is the whole difference from
 * `HtmlPreview`, where showing the artifact *is* running it and costs a paint.
 * And a script that ran and failed is the answer to the click, never an error
 * notice: a traceback is what the reader came to see.
 */

/** Newlines intact: a `<pre>` is the whole point, and the default normalizer
 *  collapses exactly the thing under test. */
const VERBATIM = {
  normalizer: getDefaultNormalizer({ collapseWhitespace: false, trim: false }),
};

const RESULT: RunFileResponse = {
  exit_code: 0,
  stdout: "1\n4\n9\n",
  stderr: "",
  written: [],
  workspace_version: "art_1",
  omitted_inputs: [],
};

function mounted(
  run: () => Promise<RunFileResponse>,
  onRan?: (result: RunFileResponse) => void,
) {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <PythonPreview
        name="sq.py"
        // `exactOptionalPropertyTypes`: an absent prop and one set to
        // `undefined` are not the same type here, so the absent case is
        // spelled by not passing it.
        {...(onRan === undefined ? {} : { onRan })}
        run={run}
        source={<pre>print(1)</pre>}
      />
    </QueryClientProvider>,
  );
}

describe("PythonPreview", () => {
  it("shows the source and starts nothing until asked", () => {
    const run = vi.fn(() => Promise.resolve(RESULT));

    mounted(run);

    expect(screen.getByText("print(1)")).toBeInTheDocument();
    // The cost being weighed is a container, not a fetch. A preview that spends
    // one on mount spends it on the reader's behalf.
    expect(run).not.toHaveBeenCalled();
  });

  it("runs once when the reader opens the output, and shows what it said", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() => Promise.resolve(RESULT));

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(await screen.findByText("1\n4\n9\n", VERBATIM)).toBeInTheDocument();
    expect(screen.getByText("运行结束，退出码 0")).toBeInTheDocument();
    expect(run).toHaveBeenCalledTimes(1);

    // Flipping back to the source and forward again is a view change, not a
    // second run: the container already ran and its answer has not changed.
    await user.click(screen.getByRole("button", { name: "源码" }));
    await user.click(screen.getByRole("button", { name: "运行结果" }));
    expect(run).toHaveBeenCalledTimes(1);
    expect(screen.getByText("1\n4\n9\n", VERBATIM)).toBeInTheDocument();
  });

  it("asks again only when the reader asks again", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() => Promise.resolve(RESULT));

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));
    await screen.findByText("1\n4\n9\n", VERBATIM);
    await user.click(screen.getByRole("button", { name: "再运行一次" }));

    await waitFor(() => {
      expect(run).toHaveBeenCalledTimes(2);
    });
  });

  it("treats a non-zero exit as the answer, not as an error", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() =>
      Promise.resolve({
        ...RESULT,
        exit_code: 1,
        stdout: "",
        stderr: 'NameError: name "x" is not defined\n',
      }),
    );

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(
      await screen.findByText('NameError: name "x" is not defined\n', VERBATIM),
    ).toBeInTheDocument();
    expect(screen.getByText("运行结束，退出码 1")).toBeInTheDocument();
    // An empty stdout is stated. A blank panel reads as "it did not run", and
    // that is a different fact from "it printed nothing".
    expect(screen.getByText("没有标准输出。")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says what a run that never happened needs, by what refused it", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() =>
      Promise.reject(new ApiError(403, "这个身份不能运行代码")),
    );

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(await screen.findByText("这个身份不能运行代码")).toBeInTheDocument();
    expect(screen.getByText(/sandbox:run/)).toBeInTheDocument();
  });

  it("prescribes nothing for a refusal this process cannot fix", async () => {
    const user = userEvent.setup();
    // A 409 is the sandbox or the workspace declining this particular call --
    // an oversized working set, an output it will not carry. It arrives with
    // the refusing side's own words, and the console adding "go check
    // code.sandbox_enabled" would be sending the reader to the one place the
    // answer is not. It used to say exactly that, for every failure.
    const run = vi.fn(() =>
      Promise.reject(new ApiError(409, "工作区超出沙箱可以携带的大小")),
    );

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(
      await screen.findByText("工作区超出沙箱可以携带的大小"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/sandbox_enabled/)).not.toBeInTheDocument();
    expect(screen.queryByText(/sandbox:run/)).not.toBeInTheDocument();
  });

  it("names the files a run put into the workspace, and tells its caller", async () => {
    const user = userEvent.setup();
    const onRan = vi.fn();
    const run = vi.fn(() =>
      Promise.resolve({ ...RESULT, written: ["out.csv"], omitted_inputs: ["big.bin"] }),
    );

    mounted(run, onRan);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(await screen.findByText(/写回工作区：out.csv/)).toBeInTheDocument();
    // The count joins the exit line, and the exit line never says 成功: a
    // script can exit 0 and write a chart whose every label is a hollow box.
    expect(screen.getByText(/退出码 0 · 写出 1 个文件/)).toBeInTheDocument();
    expect(screen.getByText(/没说明它写对了/)).toBeInTheDocument();
    // Never a silent cut: a script that could not find an input fails inside
    // itself, and its traceback would be read as the script's own bug.
    expect(screen.getByText(/big.bin/)).toBeInTheDocument();
    await waitFor(() => {
      expect(onRan).toHaveBeenCalledWith(
        expect.objectContaining({ written: ["out.csv"] }),
      );
    });
  });

  it("says a terminal program needs a terminal, above its traceback", async () => {
    const user = userEvent.setup();
    // The real thing, from a real container: an agent-written curses game.
    const run = vi.fn(() =>
      Promise.resolve({
        ...RESULT,
        exit_code: 1,
        stdout: "",
        stderr:
          'File "snake.py", line 172, in main\n    curses.wrapper(run)\n' +
          "_curses.error: setupterm: could not find terminal\n",
      }),
    );

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    // The plain sentence, and the command that actually works.
    expect(await screen.findByText(/要一个真终端/)).toBeInTheDocument();
    expect(screen.getByText("python3 sq.py")).toBeInTheDocument();
    // Above it, not instead of it: the traceback is still the evidence.
    expect(screen.getByText(/setupterm/)).toBeInTheDocument();
  });

  it("does not invent a terminal problem for an ordinary bug", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() =>
      Promise.resolve({
        ...RESULT,
        exit_code: 1,
        stdout: "",
        stderr: 'NameError: name "x" is not defined\n',
      }),
    );

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    await screen.findByText("运行结束，退出码 1");
    expect(screen.queryByText(/要一个真终端/)).not.toBeInTheDocument();
  });
});

describe("containerLimitNote", () => {
  it.each([
    ["_curses.error: setupterm: could not find terminal", "真终端"],
    ["curses.error: must call initscr() first", "真终端"],
    ["termios.error: (25, 'Inappropriate ioctl for device')", "真终端"],
    ["EOFError: EOF when reading a line", "键盘输入"],
    // The sandbox runs `--network=none` (ADR-029), so a lookup fails before
    // any socket is tried. This is the class most likely to be misread as
    // flakiness -- and a reader who believes that presses 再运行一次 forever.
    ["socket.gaierror: [Errno -2] Name or service not known", "断网"],
    ["urllib.error.URLError: <urlopen error [Errno -3]>", "断网"],
    ["OSError: [Errno 101] Network is unreachable", "断网"],
    // No display server, and the panel is a browser, so neither end could show
    // a window even if the other could.
    ["_tkinter.TclError: no display name and no $DISPLAY environment variable", "真窗口"],
    ["pygame.error: No available video device", "真窗口"],
    ["ModuleNotFoundError: No module named 'tkinter'", "真窗口"],
  ])("recognises %s", (stderr, expected) => {
    expect(containerLimitNote(stderr)).toContain(expected);
  });

  it.each([
    'NameError: name "x" is not defined',
    "ZeroDivisionError: division by zero",
    "",
    // The word appears, but as the reader's own data rather than as a failure.
    "print('curses is a python module')",
  ])("stays silent for %s", (stderr) => {
    expect(containerLimitNote(stderr)).toBeNull();
  });
});

/**
 * The other half of "why did nothing run", and the half that used to be one
 * sentence for four different causes.
 */
describe("remedyFor", () => {
  it.each([
    [503, /sandbox_enabled/],
    [403, /sandbox:run/],
    [422, /\.py/],
  ])("names the fix for a %s", (status, expected) => {
    expect(remedyFor(new ApiError(status, "refused"))).toMatch(expected);
  });

  it.each([
    // The refusing side already said what is wrong in words this process did
    // not write, and it is not this deployment's configuration.
    [new ApiError(409, "output_unsupported")],
    // Not an ApiError at all: a dropped connection, a parse failure. Inventing
    // a prescription here sends the reader somewhere the answer is not.
    [new Error("Failed to fetch")],
    [new ApiError(500, "boom")],
  ])("prescribes nothing for %s", (cause) => {
    expect(remedyFor(cause)).toBeNull();
  });
});
