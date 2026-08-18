import { useMutation } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useState } from "react";
import { ApiError } from "../api/client";
import type { RunFileResponse, WorkspaceEntryView } from "../api/types";
import { ErrorNotice, LoadingLine } from "./ui";

/**
 * A Python file, read *or run* (ADR-065).
 *
 * The sibling of `HtmlPreview`, and it exists because that sibling made an
 * asymmetry visible: an agent-built HTML page went into a frame and painted,
 * and an agent-built `.py` -- on a coding console, the likelier product of the
 * two -- could only be read. Finding out what it did meant spending a whole
 * model turn asking the agent to run it and paste the output, which is a
 * minute and a token bill to answer a question a container answers in a
 * second.
 *
 * **Where it runs is not this page.** HTML runs in the reader's own browser,
 * in an opaque origin (ADR-062); Python runs where `sandbox_run` already runs
 * it -- a throwaway container on the server, `--network=none`, nothing kept
 * between calls. This component starts one and shows what came back. That is
 * also why running is a click and never automatic, unlike the HTML frame: the
 * frame costs a paint, this costs a container start, and a preview that
 * spends one the reader did not ask for is spending it on their behalf.
 *
 * What it shows is what happened, not a rendering of it. The two streams stay
 * apart, the exit code is stated rather than translated into 成功/失败 alone,
 * and a script that raised shows its traceback -- that is the *answer* to the
 * click, not an error to hide behind a notice. What does go through
 * `ErrorNotice` is the other thing entirely: the run that never happened.
 */
export function PythonPreview({
  name,
  onRan,
  renderWritten,
  run,
  source,
}: {
  name: string;
  /** Told after every successful run, so a listing can pick up new files. */
  onRan?: (result: RunFileResponse) => void;
  /**
   * The files this run wrote, as whatever the caller shows a produced file as.
   *
   * A render prop for the same reason `source` is one: this component lives in
   * `components/` and a workspace card lives in `features/code/`, so the seam
   * runs the other way. Returning `null` is a supported answer -- the caller
   * uses it to say "the listing has not caught up, do not draw buttons that
   * cannot be clicked" -- and the plain sentence is rendered instead.
   */
  renderWritten?: (
    names: readonly string[],
    listing: readonly WorkspaceEntryView[] | undefined,
  ) => ReactNode;
  run: () => Promise<RunFileResponse>;
  /** The source view, which is what this shows until somebody runs it. */
  source: React.ReactNode;
}) {
  // Not a query. A query is a question with a stable answer that a cache may
  // serve twice; this starts a container, and the reader pressing 运行 again
  // means "again", not "tell me what it said last time".
  const running = useMutation({
    mutationFn: run,
    onSuccess: (result) => {
      onRan?.(result);
    },
  });
  // Which half is on screen. Flipped to the output by a run and back by the
  // reader, and kept as state rather than derived from `running.data` so that
  // reading the source after a run does not throw the output away.
  const [showing, setShowing] = useState<"source" | "output">("source");
  const output = showing === "output";

  return (
    <>
      <div className="aw-segmented aw-preview-views" aria-label="预览方式">
        <button
          aria-pressed={!output}
          className={output ? "" : "is-active"}
          onClick={() => {
            setShowing("source");
          }}
          type="button"
        >
          源码
        </button>
        <button
          aria-pressed={output}
          className={output ? "is-active" : ""}
          // Disabled only while one is in flight. There is nothing to show
          // before the first run, so this tab *is* the run button until there
          // is -- pressing it starts one and switches to it.
          disabled={running.isPending}
          onClick={() => {
            setShowing("output");
            if (running.data === undefined && !running.isPending) running.mutate();
          }}
          type="button"
        >
          运行结果
        </button>
        {output ? (
          <button
            className="aw-code-run-again"
            disabled={running.isPending}
            onClick={() => {
              running.mutate();
            }}
            type="button"
          >
            {running.data === undefined ? "运行" : "再运行一次"}
          </button>
        ) : null}
      </div>

      {output ? (
        <RunOutput
          error={running.isError ? running.error : null}
          name={name}
          pending={running.isPending}
          {...(renderWritten === undefined ? {} : { renderWritten })}
          result={running.data}
        />
      ) : (
        source
      )}
    </>
  );
}

/**
 * Why a run failed, when the reason is the container rather than the code.
 *
 * A sandbox run is a one-shot batch container: stdin is `/dev/null`, stdout is
 * a file, there is no tty and no `TERM` (ADR-029 -- it is a pure function, not
 * a shell). A program that wants a terminal or a person at a keyboard cannot
 * run there and never will, whatever the reader does to it. That is a
 * completely different fact from "your code has a bug", and until this existed
 * the panel presented the two identically: one block of red traceback.
 *
 * Observed, and the reason this function exists: an agent asked for 贪吃蛇 wrote
 * a perfectly good `curses` game, verified its logic in the sandbox over eight
 * runs, and said plainly that it could not check the TTY half. Clicking 运行 on
 * it then answered `_curses.error: setupterm: could not find terminal`, which
 * reads to a person as "运行坏了" -- and the feature was reported as broken.
 *
 * **Matched against CPython's own strings, never against our prose.** These
 * come out of the standard library (`curses`, `termios`) and the C library's
 * errno text; `turnBlocks.ts` refuses to parse `output_preview` for exactly the
 * opposite reason -- that text is three English sentences *this project* wrote
 * and may improve at any time. A miss here is also harmless by construction:
 * the traceback is rendered either way, so an unrecognised failure degrades to
 * precisely the behaviour that shipped before this.
 */
export function containerLimitNote(stderr: string): string | null {
  // A terminal, specifically: curses cannot initialise, or a tty-only ioctl
  // was refused. `Inappropriate ioctl for device` is ENOTTY's text and is what
  // `termios` raises against a redirected stream.
  if (
    /_?curses\.error/.test(stderr) ||
    /setupterm|could not find terminal/i.test(stderr) ||
    /termios\.error/.test(stderr) ||
    /Inappropriate ioctl for device/i.test(stderr)
  ) {
    return (
      "这个程序要一个真终端（curses/tty）才能跑。沙箱是一次性批处理容器：" +
      "没有 tty，也读不到键盘，所以它在这里跑不起来——这不是代码的错。" +
      "想真正玩它，在本机终端里运行下面这个文件。"
    );
  }
  // A person at a keyboard. `input()` against an empty stdin raises this, and
  // the traceback points at the reader's own line, which makes it look like
  // their bug rather than the container's shape.
  if (/EOFError: EOF when reading a line/.test(stderr)) {
    return (
      "这个程序在等键盘输入，而沙箱的标准输入是空的，所以它读到文件末尾就停了。" +
      "把输入写死或改从文件读，就能在这里跑；要交互地用它，请在本机终端运行。"
    );
  }
  // The network, which the sandbox does not have at all: ADR-029 runs the
  // container with `--network=none`, so a DNS lookup fails before any socket
  // is attempted. This is the class most likely to be misread as a transient
  // problem -- `Name or service not known` reads as "the network is flaky",
  // and a reader who believes that will press 再运行一次 forever.
  if (
    /socket\.gaierror|urllib\.error\.URLError|requests\.exceptions\.ConnectionError/.test(
      stderr,
    ) ||
    /Name or service not known|Temporary failure in name resolution|Network is unreachable/i.test(
      stderr,
    )
  ) {
    return (
      "这个程序要访问网络，而沙箱是断网运行的（--network=none），所以域名根本解析不了。" +
      "这不是网络不稳，重试多少次都一样。把需要的数据先写进工作区，或者在本机运行下面这个文件。"
    );
  }
  // A native window. There is no display server in the container and never
  // will be one; pygame and tkinter cannot open a window, and the panel is a
  // browser, which cannot show one either even if they could.
  if (
    /_tkinter\.TclError|ModuleNotFoundError: No module named '_?tkinter'/.test(
      stderr,
    ) ||
    /no display name and no \$DISPLAY|couldn't connect to display|videodriver|pygame\.error/i.test(
      stderr,
    )
  ) {
    return (
      "这个程序要开一个真窗口（tkinter/pygame），沙箱里没有显示器，控制台也只是个浏览器，" +
      "两边都放不下它。逻辑部分可以在这里验，画面部分只能在本机跑下面这个文件。"
    );
  }
  return null;
}

/**
 * What to tell the reader when the run never happened, by what refused it.
 *
 * Read off the HTTP status because that is the axis `routes/code.py` already
 * separates, and it separates it deliberately: a deployment with no sandbox
 * (503), a principal without `sandbox:run` (403), a name that is not Python
 * (422), and the sandbox or the workspace declining this particular call
 * (409). Four causes, four different people who can fix it.
 *
 * `null` for anything else -- a network blip, a 500 -- because the honest
 * answer there is the error message alone. A prescription invented for an
 * unrecognised failure is worse than no prescription: it sends the reader to
 * check something that was never the problem.
 */
export function remedyFor(cause: unknown): string | null {
  if (!(cause instanceof ApiError)) return null;
  if (cause.status === 503) {
    return "这个部署没有打开沙箱（code.sandbox_enabled），所以控制台跑不了任何文件。";
  }
  if (cause.status === 403) {
    return "当前身份没有 sandbox:run，跑不了工作区里的文件。";
  }
  if (cause.status === 422) {
    return "只有 .py 能这样跑；别的文件要下载下来在本机运行。";
  }
  // Deliberately no prescription. The message already carries the refusing
  // side's own words (an oversized working set, an output it will not carry),
  // and this process is not the one that has to change.
  if (cause.status === 409) return null;
  return null;
}

function RunOutput({
  error,
  name,
  pending,
  renderWritten,
  result,
}: {
  error: unknown;
  name: string;
  pending: boolean;
  renderWritten?: (
    names: readonly string[],
    listing: readonly WorkspaceEntryView[] | undefined,
  ) => ReactNode;
  result: RunFileResponse | undefined;
}) {
  // While a run is in flight the previous one stays on screen underneath, so
  // re-running does not blank a result the reader is still reading.
  if (pending && result === undefined) {
    return <LoadingLine label={`正在沙箱里运行 ${name}`} />;
  }
  if (error !== null && result === undefined) {
    const remedy = remedyFor(error);
    return (
      <>
        <ErrorNotice message={describe(error)} />
        {/* One sentence per cause, chosen by the status code the route already
            distinguishes. It used to print the same line for every failure --
            "打开 code.sandbox_enabled 并持有 sandbox:run" -- which is a correct
            prescription for two of the four cases and a confidently wrong one
            for the others: a 409 means the sandbox itself refused (an oversized
            working set, an output it would not carry) and has nothing to do
            with this deployment's settings, and telling the reader to go check
            a config flag sends them to the one place the answer is not. */}
        {remedy === null ? null : <p className="aw-page-note">{remedy}</p>}
      </>
    );
  }
  if (result === undefined) return <LoadingLine label={`正在沙箱里运行 ${name}`} />;

  // Only for a run that failed. A zero exit with something curses-shaped in
  // stderr is a program that coped, and telling it that it needed a terminal
  // it evidently did not need would be the panel inventing a problem.
  const containerLimit =
    result.exit_code === 0 ? null : containerLimitNote(result.stderr);

  return (
    <div className={`aw-code-run${pending ? " is-running" : ""}`}>
      {/* States the exit code and the count, and deliberately never the word
          成功. This line is where ADR-066's whole argument lands: a script that
          exits 0 and prints 已生成 can have written a chart whose every Chinese
          label is a hollow box, because matplotlib's default font carries no
          CJK glyphs. Exit code, stdout and stderr all said it worked. The
          strongest true thing this panel can say is the negative one -- it ran,
          it wrote these -- and the files are put directly underneath so that
          saying it costs the reader nothing. */}
      <p className="aw-code-run-exit">
        {`运行结束，退出码 ${String(result.exit_code)}`}
        {result.written.length === 0
          ? ""
          : ` · 写出 ${String(result.written.length)} 个文件`}
        {pending ? " · 正在重新运行" : ""}
      </p>
      {/* An empty stdout is said out loud. A blank panel under 运行结果 reads
          as "it did not run", and the difference between a script that printed
          nothing and a script that never started is the whole question. */}
      {result.stdout === "" ? (
        <p className="aw-page-note">没有标准输出。</p>
      ) : (
        <pre className="aw-code-file-body">{result.stdout}</pre>
      )}
      {result.stderr === "" ? null : (
        <>
          {/* Above the traceback, not instead of it. The traceback is still the
              evidence, and a reader who knows the container is the reason will
              read it differently -- looking for which line wanted the terminal
              rather than for what they got wrong. */}
          {containerLimit === null ? null : (
            <p className="aw-code-run-blocked">
              {containerLimit}
              <code>python3 {name}</code>
            </p>
          )}
          <p className="aw-code-run-label">标准错误</p>
          <pre className="aw-code-file-body is-stderr">{result.stderr}</pre>
        </>
      )}
      {/* The produced files, as the same cards a turn shows -- because a run
          that wrote `plot.png` and a turn that wrote `plot.png` produced the
          same thing, and the console had been answering the two cases very
          differently. This list used to be one grey line of names, so the only
          route to the picture a script had just drawn was to open the folded
          全部文件 list at the bottom of the panel and find it by name: two
          clicks to see the output of the click you just made. An image is
          `free` -- one decode and you know -- so its card opens itself and the
          count drops to zero.

          `renderWritten` returning null is the honest degradation, not a bug:
          the run's response and the workspace listing are two async paths, and
          a card built from a listing that has not caught up would be a button
          that does nothing. The names are still stated. */}
      {result.written.length === 0
        ? null
        : (renderWritten?.(result.written, result.files) ?? (
            <p className="aw-page-note">写回工作区：{result.written.join("、")}</p>
          ))}
      {/* Under the files, and only when there are files. It is the sentence the
          exit line above refuses to say for the reader: a program that ran is
          not a program that is right, and these are the bytes that decide. */}
      {result.written.length === 0 ? null : (
        <p className="aw-page-note">
          退出码只说明它跑完了，没说明它写对了——上面这些文件要自己看过才算数。
        </p>
      )}
      {result.omitted_inputs.length === 0 ? null : (
        // Never a silent cut. A script that could not find a file it expected
        // fails somewhere inside itself, and the traceback above would be read
        // as the script's bug rather than as a missing input.
        <p className="aw-page-note">
          工作区太大，这些文件没有一起送进沙箱：
          {result.omitted_inputs.join("、")}
        </p>
      )}
    </div>
  );
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : "运行失败";
}
