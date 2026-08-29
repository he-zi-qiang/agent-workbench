import { CircleX, Keyboard, MonitorCheck, PanelsTopLeft, ScanEye } from "lucide-react";

/**
 * What the screen tools may do to this machine, and why the order of the
 * checks is the argument (ADR-070).
 *
 * **This page explains a mechanism; it does not monitor one.** Every other
 * page here reads a live fact from an endpoint. There is no endpoint for this
 * one: the gate lives inside `agent-workbench-computer-mcp`, a stdio MCP
 * server that a Worker speaks to directly, and `apps/api` has no route that
 * can see its grants. So the session-scoped allowlist -- which applications a
 * person approved, at which tier -- is deliberately *absent* below rather than
 * illustrated with plausible rows. A screenshot of four apps captioned "这次
 * 会话批准的应用" would be read as this machine's current state, and the reader
 * has no way to tell it from one.
 *
 * What is here instead is the part that is knowable without an endpoint,
 * because it is a property of the code rather than of a session: the four
 * checks, how a tier is derived, what a refusal says, and the two ceilings on
 * a screenshot. All of it is `domain/computer.py` and `computer_mcp/gate.py`
 * restated -- if those change and this does not, this page is wrong, which is
 * the tradeoff a hand-copied explanation always carries.
 *
 * ADR-091 is the first change to have actually collected on that tradeoff, and
 * in two places at once. Check 3 now answers a weaker question, because one
 * tool changes what is frontmost instead of being judged by it; and the tier
 * row for `click` said this project could move the cursor, which it never
 * could -- `_ALLOWED` listed `mouse_move` for a gate method that did not
 * exist. The second one is the failure mode this docblock describes, arriving
 * exactly as described: a hand-copied claim outliving the thing it copied.
 */

/** The four checks, in the order `ScreenGate` applies them. */
const CHECKS = [
  {
    n: "1",
    title: "这个应用被批准过吗？",
    detail:
      "会话级 allowlist，由人一次性批准一整张名单。空名单拒绝一切——这里没有" +
      "「默认允许」可以关掉。进程重启即清空。",
    pivot: false,
  },
  {
    n: "2",
    title: "这个动作被这个 tier 允许吗？",
    detail:
      "tier 由应用自己推出，不接受申请。截图不在任何 tier 的动作表里：看见本来" +
      "就是批准这件事的目的，它由名单管，不由 tier 管。",
    pivot: false,
  },
  {
    n: "3",
    title: "它现在还在最前面吗？",
    detail:
      "每个动作发生前重读当前前台应用，从不缓存。一次决定完就动手的门禁，授权" +
      "的是它读到的那一刻的屏幕；等按键落下时，屏幕已经是它现在的样子。" +
      "ADR-091 之后这一道回答的问题弱了一档——见下一节，那是本页唯一一处" +
      "「权限边界被主动放宽」的地方。",
    pivot: true,
  },
  {
    n: "4",
    title: "之后它还在最前面吗？",
    detail:
      "只有打字需要这一道——只有打字会被打断在一半，剩下的字符跟着新窗口走。",
    pivot: false,
  },
] as const;

/** How `tier_for` classifies an application. Three rules, no exceptions branch. */
const TIERS = [
  {
    tier: "read",
    kinds: "浏览器 · 交易与钱包",
    tone: "evidence",
    detail:
      "只能出现在截图里，不点也不打字。浏览器正是密码被输入的地方；而在交易应用" +
      "里点错一下不是翻错页，是一笔委托。",
  },
  {
    tier: "click",
    kinds: "终端 · IDE",
    tone: "warning",
    detail:
      "可以左键单击、滚动，不能打字。不是因为终端危险，是因为跑命令的正门已经" +
      "存在：沙箱工具带策略门禁和审计轨迹，送进终端窗口的按键两样都没有。",
  },
  {
    tier: "full",
    kinds: "其它一切",
    tone: "success",
    detail: "无限制。这是唯一一档能打字的。",
  },
] as const;

/**
 * What `activate_application` checks, and what each check is holding back.
 *
 * The one place on this page where a boundary was deliberately *widened*, so
 * both halves are stated: what the model gained, and what it is still refused.
 */
const ACTIVATION = [
  {
    key: "target",
    title: "目标在名单里吗？",
    detail:
      "同一张 allowlist，同样从空开始。激活不看 tier——它不合成任何输入，把浏览器" +
      "切到前台买到的恰好就是「可以看它」，而看它本来就是批准它的目的。",
  },
  {
    key: "incumbent",
    title: "此刻最前面的那个也在名单里吗？",
    detail:
      "在人批准过的那一组里重排，是那个人委派出去的选择；把屏幕从正在被使用的那扇" +
      "窗抢回来，不是。所以人一切到别的窗口，任务就停住——这不是副作用，是这条规则" +
      "的正面含义。想往下走有一条正当的路：请人把那个应用也批准进来。",
  },
] as const;

/**
 * A real refusal, as `refusal()` composes it for a terminal at tier "click".
 *
 * Verbatim rather than paraphrased, because the third sentence is the whole
 * point and it is the one a paraphrase drops: without it the second reads as a
 * suggestion, and every route it forbids is one this project actually has.
 */
const REFUSAL = `"Terminal" is granted at tier "click", so type is not available for it.
Keystrokes would go to this application's command line. For shell commands,
use the sandbox tool, which runs them with a policy gate and an audit trail.
Do not attempt to work around this restriction -- never use AppleScript,
System Events, shell commands, or any other method to send input to this
application.`;

export function ComputerPage() {
  return (
    <main className="aw-utility-page aw-computer-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">屏幕控制</span>
          <h1>计算机控制</h1>
          <p>
            其它工具的作用域是这个进程自己的工作区、数据库、沙箱容器。这一个的
            作用域是运行 Worker 的那台机器本身。
          </p>
        </div>
      </header>

      {/* Before anything else, because everything below reads like a console
          otherwise. A reader who assumes this page is live will read the tier
          table as "these are the apps I granted". */}
      <div className="aw-notice is-info">
        <ScanEye aria-hidden="true" size={16} />
        <div>
          <strong>这一页说明机制，不监控运行中的会话。</strong>
          <small>
            门禁在 computer MCP 服务器进程里，agent-api 没有可以读到它的路由，
            因此「这次会话批准了哪些应用」在这里读不到，也就不显示——一张编出来
            的名单会被当成这台机器此刻的状态。下面是不依赖接口也成立的部分：
            规则本身。
          </small>
        </div>
      </div>

      <section aria-labelledby="aw-gate-heading">
        <div className="aw-section-head">
          <h2 id="aw-gate-heading">门禁四道检查</h2>
          <span>顺序就是论证</span>
        </div>
        <ol className="aw-gate-list">
          {CHECKS.map((check) => (
            <li
              className={`aw-gate-check ${check.pivot ? "is-pivot" : ""}`}
              key={check.n}
            >
              <span aria-hidden="true" className="aw-gate-number">
                {check.n}
              </span>
              <div>
                <strong>
                  {check.title}
                  {check.pivot ? <span className="aw-gate-pivot">支点</span> : null}
                </strong>
                <p>{check.detail}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className="aw-gate-note">
          第 3 道是容易漏掉、而漏掉之后无法再补上的那一道：它一旦不成立，上面
          两道检查的都是另一块屏幕。
        </p>
        <p className="aw-gate-note">
          第 3 道和真正动手之间还有一步，它<strong>不是</strong>一道检查：模型是在
          某一块显示器的截图上量的坐标，而事件被发到一个横跨所有显示器的全局空间
          （ADR-090）。单屏时两者是同一个空间，所以这个区别曾经一年不可见；接上
          第二块屏，照着它的截图量出来的坐标会落在主屏上——点击成功、位置错、
          什么都不报。因此坐标带着它是在哪块屏上量的走，不在那块屏上的坐标被拒绝，
          而只要这台机器不止一块屏，省略这件事本身也被拒绝。这一步排在三道检查
          之后，好让一个什么都没被批准的会话没法用它量出别人的显示器摆位。
        </p>
      </section>

      {/* Between the four checks and the tier table on purpose: it is a
          footnote to check 3, and reading it after the tiers would make it
          look like a fourth tier. */}
      <section aria-labelledby="aw-activate-heading">
        <div className="aw-section-head">
          <h2 id="aw-activate-heading">换到另一个应用</h2>
          <span>ADR-091 · 第 3 道检查的含义变了</span>
        </div>
        <div className="aw-notice is-warning">
          <PanelsTopLeft aria-hidden="true" size={16} />
          <div>
            <strong>
              第 3 道从「人选了这扇窗」变成「模型在人批准的集合里选了一扇」。
            </strong>
            <small>
              在此之前，六个工具没有一个能改变前台应用，而每一个都要求被批准的应用
              此刻在最前面——于是任何跨两个应用的任务都走不到第二步，而且不是被
              拒绝，是没有工具可调。加上激活之后，「前台是被批准的」不再等于
              「人此刻正看着它并选择了它」。丢掉的就是这一层信息。
            </small>
          </div>
        </div>
        <ol className="aw-gate-list">
          {ACTIVATION.map((check, index) => (
            <li
              className={`aw-gate-check ${index === 1 ? "is-pivot" : ""}`}
              key={check.key}
            >
              <span aria-hidden="true" className="aw-gate-number">
                {index + 1}
              </span>
              <div>
                <strong>
                  {check.title}
                  {index === 1 ? <span className="aw-gate-pivot">收窄</span> : null}
                </strong>
                <p>{check.detail}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className="aw-gate-note">
          第二条的拒绝文案<strong>不说最前面的是谁</strong>。它恰好只在「最前面的
          应用没被批准」时触发，所以一个会点名的拒绝，等于把每一次被拒的激活变成
          一次「此刻这个人在用什么」的读数——那正是名单要挡的东西。同样的理由，
          读名单的那个工具只答「有没有一个名单里的在最前面」，不答没有的时候是谁。
        </p>
        <p className="aw-gate-note">
          激活<strong>从不启动应用</strong>。启动一个进程和把窗口重排不是一个量级
          的行为，而且人批准的那份名单不是这个意思：对话框问的是「可以在这次会话里
          控制下列应用」。两个应用都得已经开着，任务才跨得过去——代价记在
          known-gaps F-29。
        </p>
      </section>

      <section aria-labelledby="aw-tier-heading">
        <div className="aw-section-head">
          <h2 id="aw-tier-heading">tier 怎么定的</h2>
          <span>从应用本身推出，不接受申请</span>
        </div>
        <div className="aw-tier-table">
          {TIERS.map((row) => (
            <div className="aw-tier-row" key={row.tier}>
              <span className={`aw-tier-badge is-${row.tone}`}>{row.tier}</span>
              <div>
                <strong>{row.kinds}</strong>
                <p>{row.detail}</p>
              </div>
            </div>
          ))}
        </div>
        <p className="aw-gate-note">
          判定先查 bundle id，再按长度从长到短查名字子串——两个都要。bundle id
          精确但不完整，名字完整但可以伪造；只认 bundle id 的话，这个项目没听说过
          的新浏览器反而会落到 full。子串按长度排序，是为了让「chrome remote
          desktop」不被「chrome」先接走。
        </p>
      </section>

      <section aria-labelledby="aw-refusal-heading">
        <div className="aw-section-head">
          <h2 id="aw-refusal-heading">被拒绝的时候说什么</h2>
          <span>三段，第三段才是关键</span>
        </div>
        <div className="aw-refusal">
          <div className="aw-refusal-head">
            <CircleX aria-hidden="true" size={15} />
            <strong>向 Terminal 打字</strong>
            <code>tier click · type</code>
          </div>
          <pre>{REFUSAL}</pre>
          <p>
            只说「不行」的拒绝会被绕过——模型接下来会去试 AppleScript。所以第二段
            指出被认可的那条路，第三段把绕行明确禁掉：没有第三段，第二段读起来
            像建议。
          </p>
        </div>
      </section>

      <div className="aw-computer-pair">
        <section className="aw-fact-card">
          <MonitorCheck aria-hidden="true" size={16} />
          <strong>截图的两个上限</strong>
          <p>
            边长 1568 px 是视觉编码器自己会降采样的点，超过就是在为被丢掉的像素
            付传输费；token 1568 是这一轮对话付得起的。宽屏上先咬的是后者：
            1568×1568 在边长之内，却要 3136 个 token，是上限的两倍。
          </p>
          <p className="aw-fact-note">
            缩放用二分搜索而不是解析解——两个上限在不同屏幕上先后生效，而取整到
            整像素会让算出来「刚好装得下」的尺寸进位成装不下的，那是一张编码完
            才被拒绝的图。宽高比永远保持：截图是模型随后要在里面点击的坐标系。
          </p>
        </section>

        <section className="aw-fact-card">
          <Keyboard aria-hidden="true" size={16} />
          <strong>打字之后还要再数一次</strong>
          <p>
            按键跟着键盘焦点走。一个窗口在字符串打到一半时抢到前台，剩下的字符
            就跟着它走——同一串字落进两个应用，其中只有一个被批准过。
          </p>
          <p className="aw-fact-note">
            所以适配器报告送达了多少个字符，门禁用这个数字回话，而不是回一句
            denied：只被告知 denied 的模型会重打整串，于是前半段到两次。
          </p>
        </section>
      </div>
    </main>
  );
}
