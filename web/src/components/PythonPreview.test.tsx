import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getDefaultNormalizer, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RunFileResponse } from "../api/types";
import { PythonPreview } from "./PythonPreview";

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

  it("says what a run that never happened needs", async () => {
    const user = userEvent.setup();
    const run = vi.fn(() => Promise.reject(new Error("这个部署不能运行代码")));

    mounted(run);
    await user.click(screen.getByRole("button", { name: "运行结果" }));

    expect(await screen.findByText("这个部署不能运行代码")).toBeInTheDocument();
    // The two causes have two different fixes, and neither of them is "retry".
    expect(screen.getByText(/sandbox:run/)).toBeInTheDocument();
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
    // Never a silent cut: a script that could not find an input fails inside
    // itself, and its traceback would be read as the script's own bug.
    expect(screen.getByText(/big.bin/)).toBeInTheDocument();
    await waitFor(() => {
      expect(onRan).toHaveBeenCalledWith(
        expect.objectContaining({ written: ["out.csv"] }),
      );
    });
  });
});
