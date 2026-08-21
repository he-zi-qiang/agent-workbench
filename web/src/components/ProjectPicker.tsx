/**
 * 这一件东西属于哪件事。
 *
 * 归属是**它自己的属性**，所以这个控件长在它自己的头部，而不是长成侧栏每一行上
 * 的第三个图标——行动作已经有改名和删除两个，第三个会把一列本来很安静的行挤成
 * 一排按钮。
 *
 * 三处和 ADR-071 一一对应的地方：
 *
 * * 第一项是「不属于任何项目」，而且它是**默认选中**的那一项。归属可空，空是
 *   正常状态；把这一项做成「清除」式的次要动作，等于说没有归属是一种异常。
 * * 一个项目都没有的时候，这个控件不出现。一个只能选「无」的下拉框是在提醒读者
 *   他缺了一个东西，而他并不缺。
 * * 失败就地说，不吞。改归属是一次写，写失败了读者需要知道。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { listProjects } from "../api/client";
import type { PrincipalIdentity } from "../api/types";

/** 「不属于任何项目」在 `<select>` 里的值。`""` 而不是 `"null"`：它是空，不是一个名字。 */
const NONE = "";

export function ProjectPicker({
  identity,
  projectId,
  onAssign,
  label,
}: {
  identity: PrincipalIdentity;
  /** 当前归属，`null` 表示不属于任何项目。 */
  projectId: string | null;
  /** `null` 表示拿出来。抛出的错误由这里就地显示。 */
  onAssign: (projectId: string | null) => Promise<void>;
  label: string;
}) {
  const queries = useQueryClient();
  const projects = useQuery({
    queryKey: ["projects", identity],
    queryFn: ({ signal }) => listProjects(identity, { signal }),
  });

  const assign = useMutation({
    mutationFn: (next: string | null) => onAssign(next),
    onSuccess: () => {
      void queries.invalidateQueries({ queryKey: ["project-items", identity] });
    },
  });

  const change = useCallback(
    (value: string) => {
      assign.mutate(value === NONE ? null : value);
    },
    [assign],
  );

  // 一个项目都没有时不画这个控件：一个只能选「无」的下拉框，是在提醒读者他缺了
  // 一个东西，而归属本来就是可选的。
  if (projects.data === undefined || projects.data.projects.length === 0) {
    return null;
  }

  return (
    <span className="aw-project-picker">
      <label className="aw-sr-only" htmlFor="aw-project-picker">
        {label}
      </label>
      <select
        disabled={assign.isPending}
        id="aw-project-picker"
        onChange={(event) => change(event.target.value)}
        value={projectId ?? NONE}
      >
        <option value={NONE}>不属于任何项目</option>
        {projects.data.projects.map((project) => (
          <option key={project.project_id} value={project.project_id}>
            {project.name}
          </option>
        ))}
      </select>
      {assign.isError ? (
        <small role="alert">
          {assign.error instanceof Error ? assign.error.message : "没能改成功"}
        </small>
      ) : null}
    </span>
  );
}
