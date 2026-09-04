/**
 * 一轮为什么停下，说给正在等它的人听。
 *
 * 从 `CodePage` 里搬出来，因为现在有两处要说这句话：一轮**正在**跑的时候停下
 * （页面上那条 fault），和一轮**早就**停过、读者回来看转录的时候（`CodeTurn`
 * 的那一行）。此前只有前者：一段会话里三轮因为模型服务 400 而失败，回来看到的
 * 是三张只有指令、脚注写着 0 → 0 的卡片，什么也没说——用户的原话是「多次的调用
 * 和回答，展现的不是很好」。
 */

/**
 * One sentence for a turn that stopped without a report.
 *
 * The vocabulary is the runtime's `StopReason`; what every branch has to say
 * is the same two things -- the work so far is safe (writes land per write,
 * not at the end), and the way forward is to just keep talking. A stopped
 * turn used to render as nothing at all, which read as a broken session.
 *
 * `StopReason` alone turned out not to be enough vocabulary (ADR-0084). Every
 * provider failure arrives here as `"error"`, so the last branch was rendering
 * `这一轮没有跑完（error）` for an exhausted account, a rejected key, a retired
 * model id and a 500 alike -- four different things to go do. The failure's
 * own code and message are now passed alongside, and read first.
 */
export function stopNote(
  reason: string,
  errorCode?: string | null,
  errorMessage?: string | null,
): string {
  if (errorCode === "provider_account_rejected") {
    // Ahead of the `reason` branches because this one contradicts them. The
    // other notes end with 「直接说下一步就能继续」, and that advice is wrong
    // here for the same reason it was wrong for `context_limit`: the next turn
    // in this session calls the same account and fails the same way. Nothing
    // inside this console can fix it.
    return "模型服务拒绝了这个部署的账号：余额用尽，或者密钥失效。已完成的改动都在工作区里；重试没有用，要先去模型服务商那边充值或者换一把密钥。";
  }
  if (reason === "deadline") {
    return "这一轮到时间停下了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "max_steps" || reason === "max_tool_calls") {
    return "这一轮把步数用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "token_budget" || reason === "cost_budget") {
    return "这一轮把预算用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "context_limit") {
    // ADR-0080。这一条和上面几条不一样：它不是"额度用完了"，而是"这段对话本身
    // 长到装不下了"，所以「直接说下一步就能继续」在这里是错的建议——同一个会话
    // 的下一轮会带着同样长的历史再撞一次。
    return "这一轮的对话长到模型装不下了。已完成的改动都在工作区里；开一个新会话继续，或者把要做的事拆小一点。";
  }
  if (reason === "cancelled") {
    return "这一轮被取消了。已完成的改动都在工作区里。";
  }
  // The message, then the code, then the bare stop reason. `explainFailure`
  // settled this rule for Task details and it holds here: a string this
  // function has no words for is still the most specific thing anyone has,
  // and `error` is the least specific thing there is. The message is the
  // server's English -- shown rather than dropped, because a reader chasing a
  // provider fault would otherwise have to go read the event log to learn
  // which one it was.
  const detail = errorMessage ?? errorCode ?? reason;
  return `这一轮没有跑完（${detail}）。已完成的改动都在工作区里，直接说下一步就能继续。`;
}
