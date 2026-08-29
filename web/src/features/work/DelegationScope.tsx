import type { DelegationCapabilities } from "../../api/types";

/**
 * 这台部署会不会让下一个任务派子代理，派得起几个。
 *
 * **它读的是配置，写的是范围，而不是一组可以调的旋钮。** 派给谁、一轮派几个，
 * 是模型在运行途中用 `delegate_agent` 决定的；图的形状在提交那一刻就冻住了，
 * 委派发生在某一个节点的运行**内部**。所以这里没有「先摆好三个 agent 再开跑」
 * 这一步可做，能说的只有它能选的范围有多宽——这也是为什么这一块是只读的散文
 * 而不是四个输入框：一个能填的框会承诺一件这个控制平面做不到的事。
 *
 * **关掉的时候不显示那几个数。** 服务端在委派关闭时把三个树上限送成 1、token
 * 上限送成 0，那是「这棵树不存在」而不是「这棵树只剩一格」。把它们当上限画出来，
 * 读者会读成一台配得极紧的部署。所以这两种状态各说各的话。
 *
 * **读不到的时候说读不到。** 这一块加载失败时最省事的做法是什么都不画，而那正是
 * `RunPanel` 那条被批评过的做法：沉默同时表示「这里没有」和「这里没答上来」。
 * 一行字比一片空白诚实，代价是一行字。
 */
export function DelegationScopeNote({
  delegation,
}: {
  delegation: DelegationCapabilities | undefined;
}) {
  if (delegation === undefined) {
    return (
      <p className="aw-delegation-scope is-unknown">
        读不到这台部署的委派设置，所以这里不说它开没开。
      </p>
    );
  }

  if (!delegation.enabled) {
    return (
      <p className="aw-delegation-scope">
        这台部署<strong>没有开委派</strong>，所以任务只会有一个运行。
      </p>
    );
  }

  return (
    <div className="aw-delegation-scope is-enabled">
      {/* 整句不折行。JSX 把源码里的换行加缩进折成**一个空格**，而中文两个汉字
          之间的空格是看得见的：折在「运行途中／决定」之间就渲染成「运行途中 决
          定」，折在句号后面就渲染成「委派。 派给谁」——全角句号后面本来就不该
          再有空格。这一条不是本文件独有的，见 `docs/known-gaps.md` 里同名条目。 */}
      <p>
        这台部署<strong>允许委派</strong>。派给谁、一轮派几个由模型在运行途中决定；下面是它能选的范围。
      </p>
      <ul>
        <li>
          <span>一次运行最多派</span>
          <strong>{delegation.max_children_per_run} 个</strong>
          <small>整个运行累计，不是同时</small>
        </li>
        <li>
          <span>同时在跑最多</span>
          <strong>{delegation.max_parallel_child_invocations} 个</strong>
          <small>独立的池子，不占主运行的并发位</small>
        </li>
        <li>
          <span>每个子代理最多烧</span>
          <strong>{formatTokens(delegation.max_tokens_per_agent_invocation)}</strong>
          <small>主运行的额度看不见这一笔</small>
        </li>
        <li>
          <span>委派深度</span>
          <strong>{delegation.max_delegation_depth}</strong>
          <small>
            {delegation.max_delegation_depth === 1
              ? "子代理不能再往下派"
              : "子代理还可以继续往下派"}
          </small>
        </li>
      </ul>
    </div>
  );
}

/**
 * `120000` → `120k`。
 *
 * 和 `RunPanel` 那份是同一条规则，刻意重复而不是抽出去共用：那一份格式化的是
 * **花掉了多少**，会每两秒变一次，所以它把 1000 以下留成原样以免读者盯着一个
 * 抖动的小数；这一份格式化的是一个配置常量，永远不动。两处今天恰好同形，但它们
 * 不为同一件事负责，合并会让改其中一个的人以为自己只改了一个。
 */
function formatTokens(value: number): string {
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(0)}k`;
}
