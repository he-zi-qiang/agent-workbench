/**
 * 一段推理，折成它的第一句。
 *
 * 从 `CodeTurn` 里搬出来，因为 Task 与 Chat 的时间线（`StepStream`）现在也这么折：
 * 此前那边是「思路摘要 · 前 176 个字…」一行硬截，正文在下面那层「思考摘要」里再
 * 整段出现一次——同一段话长短两版并存。折叠和截断的差别在于丢不丢：截断把读者要
 * 的那一半扔了，折叠只把它放到一次点击之后。
 *
 * ## 拉丁文和汉字不是同一把尺
 *
 * 上限此前只有一个数：120 个**字符**。它是按汉字定的——120 个汉字在 820px 的正文
 * 里是两到三行——而模型的推理常常是英文：DeepSeek 在这个控制台上写的推理十有八九
 * 是英文。120 个拉丁字母只有一行半，而英文一句话常常不止这么长，于是摘要在第 120 个
 * 字符上硬切，实测切在 `Redis dis` 这种词的中间。汉字没有这个问题，因为汉字没有词。
 *
 * 所以现在两把尺：拉丁文上限 200（一个拉丁字母约半个汉字宽，行数相当），并且找不到
 * 句号时退到**最后一个词边界**而不是最后一个字符。哪把尺由前 120 个字符里字母和汉字
 * 谁多决定——「先看看 notes.md 里有什么」仍然按汉字算，`notes.md` 只是一个名字。
 */

/** 汉字推理的摘要上限：120 个汉字在 820px 的正文里是两到三行。 */
export const THOUGHT_HEAD_MAX = 120;

/** 拉丁文推理的摘要上限：同样的两三行装得下的字符数。 */
export const THOUGHT_HEAD_MAX_LATIN = 200;

/**
 * Split a thought into the line that stands for it and the rest.
 *
 * A newline first, because a model writing reasoning uses its own line breaks
 * and the first line is the heading it wrote for itself. Then a Chinese
 * sentence mark. An ASCII sentence mark only when whitespace follows it --
 * bare `.` would cut `notes.md` and `0.5` in half. Only then the limit, and
 * for Latin prose the limit backs up to the last word boundary.
 */
export function splitThought(text: string): { head: string; body: string } {
  const trimmed = text.trim();
  const latin = isMostlyLatin(trimmed.slice(0, THOUGHT_HEAD_MAX));
  const limit = latin ? THOUGHT_HEAD_MAX_LATIN : THOUGHT_HEAD_MAX;
  const window = trimmed.slice(0, limit);
  let end = window.indexOf("\n");
  if (end === -1) {
    for (const mark of ["。", "！", "？"]) {
      const at = window.indexOf(mark);
      if (at !== -1 && (end === -1 || at < end)) end = at;
    }
    if (end !== -1) end += 1;
  }
  if (end === -1) {
    const match = /[.!?](?=\s)/.exec(window);
    if (match !== null) end = match.index + 1;
  }
  // No ellipsis when it is cut hard: the disclosure triangle already says
  // there is more underneath.
  if (end === -1) {
    if (trimmed.length <= limit) {
      end = trimmed.length;
    } else if (latin) {
      // 词边界，但不早于窗口的一半：一个占了大半个窗口的长 URL 会把摘要缩成
      // 前面那两个词，那时切在字符上反而更像一句话。
      const lastSpace = Math.max(window.lastIndexOf(" "), window.lastIndexOf("\t"));
      end = lastSpace > limit / 2 ? lastSpace : limit;
    } else {
      end = limit;
    }
  }
  return {
    head: trimmed.slice(0, end).trim(),
    body: trimmed.slice(end).trim(),
  };
}

/** 字母比汉字多，就按拉丁文的尺量。 */
function isMostlyLatin(sample: string): boolean {
  const han = (sample.match(/\p{Script=Han}/gu) ?? []).length;
  const letters = (sample.match(/[A-Za-z]/g) ?? []).length;
  return letters > han;
}
