import { CircleSlash, MonitorDot, ShieldCheck } from "lucide-react";

import type {
  ComputerAction,
  ComputerSessionResponse,
} from "../../api/types";

/**
 * 屏幕控制服务器此刻的样子：批准了哪些应用、前台是谁、最近做过什么。
 *
 * **这一页此前一个接口都不读**，而那是被论证过的克制：门禁活在另一个进程里，
 * `apps/api` 没有能看到它的路由，所以与其画一张编出来的名单，不如什么都不画——
 * 「一张四个应用的截图配上『这次会话批准的应用』，会被读成这台机器此刻的状态」。
 * ADR-095 把那条路修出来了，所以这块面板画的是真的。
 *
 * **它说出前台应用的名字，哪怕没被批准。** 同一时刻模型收到的每一句拒绝都不说。
 * 两者是同一条规则的两个读者：这块面板的读者就坐在那扇窗前面，看得见它；而他要做的
 * 判断恰恰是「要不要把我正在用的这个也批准进来」，一块只肯说「不在名单里的某个应用」
 * 的面板会把那个判断变成猜谜。
 *
 * **三种状态，不是两种。** 那台服务器默认不启动，所以「读不到」是普通机器上的普通
 * 答案，而不是故障——它必须和「跑着、但没人批准任何应用」长得不一样。把这两种画成
 * 同一片空白，就又回到了当初拒绝画假名单的那个问题上。
 */
export function ScreenSessionPanel({
  data,
  loading,
}: {
  data: ComputerSessionResponse | undefined;
  loading: boolean;
}) {
  if (data === undefined) {
    return (
      <div className="aw-screen-panel is-quiet">
        <p>{loading ? "正在问这台机器……" : "读不到屏幕控制服务器的状态。"}</p>
      </div>
    );
  }

  if (!data.reachable) {
    // 不是错误状态，所以不用警告色。这台服务器默认不启动，绝大多数时候它就该是这样。
    return (
      <div className="aw-screen-panel is-quiet">
        <p>
          <strong>屏幕控制服务器没有在跑。</strong>
          {data.detail === "" ? null : ` ${data.detail}`}
        </p>
        <p className="aw-screen-hint">
          它不由任何一条常规启动路径带起来——要它在，得单独起：
          <code>scripts/dev.sh computer-server</code>。没有它，下面这些规则照样成立，只是这台机器上没有任何会话可看。
        </p>
      </div>
    );
  }

  const session = data.session;
  if (session === null) {
    return (
      <div className="aw-screen-panel is-quiet">
        <p>服务器答了，但没有给出会话内容。</p>
      </div>
    );
  }

  return (
    <div className="aw-screen-panel">
      <div className="aw-screen-head">
        <span className="aw-screen-live">
          <span aria-hidden="true" className="aw-screen-dot" />
          正在应答
        </span>
        {/* 「这个进程」不是「这次会话」。门禁的 allowlist 是进程级的（F-19），而一块
            写着「这次会话」的面板会是第一个把会话级 grant 读进存在的地方。 */}
        <span className="aw-screen-scope">
          批准挂在这个<strong>进程</strong>上，进程一关就清空
        </span>
      </div>

      <div className="aw-screen-split">
        <section className="aw-screen-block">
          <h3>
            <ShieldCheck aria-hidden="true" size={14} />
            批准了 {session.granted.length} 个应用
          </h3>
          {session.granted.length === 0 ? (
            // 和「服务器没在跑」分得开的那一半：它跑着，只是还没有人批准任何东西。
            <p className="aw-screen-hint">
              还没有任何应用被批准。空名单拒绝一切——这里没有「默认允许」可以关掉。
            </p>
          ) : (
            <ul className="aw-screen-grants">
              {session.granted.map((grant) => (
                <li key={grant.bundle_id}>
                  <span className={`aw-tier-badge is-${TIER_TONE[grant.tier] ?? "evidence"}`}>
                    {grant.tier}
                  </span>
                  <strong>{grant.name}</strong>
                  <code>{grant.bundle_id}</code>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="aw-screen-block">
          <h3>
            <MonitorDot aria-hidden="true" size={14} />
            此刻最前面的
          </h3>
          <p className="aw-screen-frontmost">
            <strong>{session.frontmost.name}</strong>
            <code>{session.frontmost.bundle_id}</code>
          </p>
          <p className={`aw-screen-hint${session.frontmost.granted ? "" : " is-blocking"}`}>
            {session.frontmost.granted
              ? "它在名单里，所以第 3 道检查此刻是过的。"
              : "它不在名单里，所以此刻任何动作都会被第 3 道拦下——任务会停在这里等你。"}
          </p>
          {session.frontmost.granted ? null : (
            <p className="aw-screen-hint">
              {/* 这一句是这块面板存在的理由。模型收到的拒绝里没有这个名字，人这里有，
                  因为要做决定的是人，而那扇窗就在他眼前。 */}
              模型收到的拒绝里<strong>没有</strong>这个名字——它只被告知「前台不在名单里」。你看得见它，是因为这块面板的读者是你。
            </p>
          )}
        </section>
      </div>

      <section className="aw-screen-block">
        <h3>最近做过什么</h3>
        {session.actions.length === 0 ? (
          <p className="aw-screen-hint">这个进程还没有被要求对屏幕做任何事。</p>
        ) : (
          <ol className="aw-screen-actions">
            {[...session.actions].reverse().map((action, index) => (
              <ActionRow action={action} key={`${action.at}#${String(index)}`} />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

/** tier 到它在这一页上一直用着的那三档色。 */
const TIER_TONE: Readonly<Record<string, string>> = {
  read: "evidence",
  click: "warning",
  full: "success",
};

function ActionRow({ action }: { action: ComputerAction }) {
  return (
    <li className={action.allowed ? "" : "is-refused"}>
      <span className="aw-screen-when">{formatTime(action.at)}</span>
      {action.allowed ? null : (
        <CircleSlash aria-hidden="true" className="aw-screen-refused" size={13} />
      )}
      <code className="aw-screen-verb">{action.action}</code>
      {action.application === null ? null : (
        <span className="aw-screen-on">{action.application.name}</span>
      )}
      {action.detail === "" ? null : (
        <span className="aw-screen-detail">{action.detail}</span>
      )}
      {action.reason === "" ? null : (
        // 逐字，不改写。这句话就是模型读到的那一句——两边看到的是同一句，
        // 面板才说得清「它为什么停在这里」。
        <span className="aw-screen-reason">{action.reason}</span>
      )}
    </li>
  );
}

/**
 * `2026-08-29T11:20:18Z` → `19:20:18`。
 *
 * 到秒，而不是像别处那样到分：这一列里连着几行常常发生在同一分钟内，而读者要看的
 * 恰恰是它们的先后。
 */
function formatTime(value: string): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(at);
}
