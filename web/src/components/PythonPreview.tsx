import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { RunFileResponse } from "../api/types";
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
  run,
  source,
}: {
  name: string;
  /** Told after every successful run, so a listing can pick up new files. */
  onRan?: (result: RunFileResponse) => void;
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
          result={running.data}
        />
      ) : (
        source
      )}
    </>
  );
}

function RunOutput({
  error,
  name,
  pending,
  result,
}: {
  error: unknown;
  name: string;
  pending: boolean;
  result: RunFileResponse | undefined;
}) {
  // While a run is in flight the previous one stays on screen underneath, so
  // re-running does not blank a result the reader is still reading.
  if (pending && result === undefined) {
    return <LoadingLine label={`正在沙箱里运行 ${name}`} />;
  }
  if (error !== null && result === undefined) {
    return (
      <>
        <ErrorNotice message={describe(error)} />
        {/* Named because the two causes have two different fixes, and neither
            is "try again": one is a setting, the other is a scope. */}
        <p className="aw-page-note">
          运行需要这个部署打开沙箱（code.sandbox_enabled），并且当前身份持有
          sandbox:run。
        </p>
      </>
    );
  }
  if (result === undefined) return <LoadingLine label={`正在沙箱里运行 ${name}`} />;

  return (
    <div className={`aw-code-run${pending ? " is-running" : ""}`}>
      <p className="aw-code-run-exit">
        {result.exit_code === 0
          ? "运行结束，退出码 0"
          : `运行结束，退出码 ${String(result.exit_code)}`}
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
          <p className="aw-code-run-label">标准错误</p>
          <pre className="aw-code-file-body is-stderr">{result.stderr}</pre>
        </>
      )}
      {result.written.length === 0 ? null : (
        <p className="aw-page-note">
          写回工作区：{result.written.join("、")}
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
