/**
 * 一段推理，折成它的第一句。
 *
 * 从 `CodeTurn` 里搬出来，因为 Task 与 Chat 的时间线（`StepStream`）现在也这么折：
 * 此前那边是「思路摘要 · 前 176 个字…」一行硬截，正文在下面那层「思考摘要」里再
 * 整段出现一次——同一段话长短两版并存。折叠和截断的差别在于丢不丢：截断把读者要
 * 的那一半扔了，折叠只把它放到一次点击之后。
 */

/** How long a folded thought's first line may be before it stops being a line. */
export const THOUGHT_HEAD_MAX = 120;

/**
 * Split a thought into the line that stands for it and the rest.
 *
 * A newline first, because a model writing reasoning uses its own line breaks
 * and the first line is the heading it wrote for itself. Then a Chinese
 * sentence mark. An ASCII full stop only when whitespace follows it -- bare
 * `.` would cut `notes.md` and `0.5` in half.
 */
export function splitThought(text: string): { head: string; body: string } {
  const trimmed = text.trim();
  const window = trimmed.slice(0, THOUGHT_HEAD_MAX);
  let end = window.indexOf("\n");
  if (end === -1) {
    for (const mark of ["。", "！", "？"]) {
      const at = window.indexOf(mark);
      if (at !== -1 && (end === -1 || at < end)) end = at;
    }
    if (end !== -1) end += 1;
  }
  if (end === -1) {
    const match = /\.\s/.exec(window);
    if (match !== null) end = match.index + 1;
  }
  // No ellipsis when it is cut hard: the disclosure triangle already says
  // there is more underneath.
  if (end === -1) {
    end =
      trimmed.length <= THOUGHT_HEAD_MAX ? trimmed.length : THOUGHT_HEAD_MAX;
  }
  return {
    head: trimmed.slice(0, end).trim(),
    body: trimmed.slice(end).trim(),
  };
}
