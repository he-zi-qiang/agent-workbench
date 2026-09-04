import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { getDeploymentCapabilities } from "../../api/client";
import type { DeploymentCapability } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";

/**
 * 这台部署里，一段编码会话够得到什么（ADR-0109）。
 *
 * 一行小字，放在起始屏的标题下面、输入框上面，在第一句指令发出去**之前**。
 * 它存在是因为一件实测的事：Windows 的容器栈上 Code 是开的，而沙箱、宿主命令、
 * 联网搜索一件都没有，读者知道这一点的唯一途径是模型自己在回合里写「本环境
 * 没有 shell 与网络」——那句话读起来像模型偷懒，不像一个关于部署的事实，
 * 于是人去查 key、查网络、换模型。同一份答案「运行状态」页早就有了，但没人
 * 会在开始编码之前先去读运行状态。
 *
 * 只画三件，而且是三件**可以缺席**的：沙箱运行、宿主命令、联网搜索。读写项目
 * 文件不在里面——有 Code 就有它，一个永远亮着的项没有信息。缺席的那几件画成
 * 划掉的灰字，原因放在 title 里，整行末尾指向运行状态页，补法在那边。
 *
 * 读的是 `/v1/system/capabilities` 那份清单里的三行，不另开接口：三个答案
 * 都是装配期的事实，那份清单就是它们唯一的出处（ADR-102）。读不到（旧 API、
 * 网络）就什么也不画——这一行是提示，不是门。
 */
const REACH_ROWS: ReadonlyArray<readonly [string, string]> = [
  ["code.sandbox", "沙箱运行"],
  ["code.host_commands", "宿主命令"],
  ["code.web_search", "联网搜索"],
];

export function CodeReach() {
  const { identity } = useIdentity();
  const report = useQuery({
    queryKey: ["deployment-capabilities", identity.tenantId, identity.principalId],
    queryFn: () => getDeploymentCapabilities(identity),
    staleTime: Infinity,
    retry: false,
  });

  const rows = new Map<string, DeploymentCapability>(
    (report.data?.capabilities ?? []).map((row) => [row.id, row]),
  );
  const items = REACH_ROWS.flatMap(([id, label]) => {
    const row = rows.get(id);
    return row === undefined ? [] : [{ id, label, row }];
  });
  if (items.length === 0) return null;

  const missing = items.filter((item) => item.row.state !== "available");

  return (
    <p className="aw-code-reach" aria-label="这台部署里编码会话能碰到什么">
      <span className="aw-code-reach-lead">这里能碰到</span>
      {items.map((item) => (
        <span
          className={`aw-code-reach-item${
            item.row.state === "available" ? "" : " is-absent"
          }`}
          key={item.id}
          title={item.row.state === "available" ? undefined : item.row.reason}
        >
          {item.label}
        </span>
      ))}
      {/* 只有真的缺了东西才给链接：一行全亮的提示后面挂一个「去补」是在
          让人去找不存在的活。 */}
      {missing.length > 0 ? (
        <Link className="aw-code-reach-link" to="/system">
          缺的怎么补 →
        </Link>
      ) : null}
    </p>
  );
}
