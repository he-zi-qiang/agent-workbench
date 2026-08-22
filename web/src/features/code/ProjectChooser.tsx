import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderPlus, Folder } from "lucide-react";
import { useState } from "react";
import { createProjectAtDirectory, listProjects } from "../../api/client";
import type { ProjectView } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { ErrorNotice, LoadingLine } from "../../components/ui";
import { FolderPicker } from "./FolderPicker";

/**
 * 开始编码之前要回答的那一个问题：在哪个文件夹里（ADR-074）。
 *
 * 这是 Code 的门，不是一个可以跳过的设置项。ADR-074 §7.1 那条不变量——每个编码
 * 会话都属于一个有目录的项目——只有在这里强制才成立：允许「先开始、回头再选」的
 * 界面，会让「产物存哪了」重新有两个答案，而人问这句话的时候通常已经在找不到
 * 东西了。
 *
 * 只列**有目录的**项目。没有目录的项目在 ADR-074 之后不该存在，但库里可能还留着
 * 早先建的；把它们列出来会让人选中一个然后打不开，而那看起来像是坏了。
 */
export function ProjectChooser({
  onChoose,
}: {
  onChoose: (project: ProjectView) => void;
}) {
  const { identity } = useIdentity();
  const queries = useQueryClient();
  const [picking, setPicking] = useState(false);

  const projects = useQuery({
    queryKey: ["projects", identity],
    queryFn: ({ signal }) => listProjects(identity, { signal }),
  });

  const open = useMutation({
    mutationFn: (rootPath: string) =>
      createProjectAtDirectory(
        identity,
        // 按文件夹名命名，和 CLI 一致。文件夹就是项目，再问一次名字是在问一个
        // 屏幕上已有答案的问题；改名的入口在别处，不该挡在开始之前。
        rootPath.split("/").filter(Boolean).pop() ?? rootPath,
        rootPath,
      ),
    onSuccess: async (project) => {
      await queries.invalidateQueries({ queryKey: ["projects", identity] });
      onChoose(project);
    },
  });

  const withFolder = (projects.data?.projects ?? []).filter(
    (project) => project.root_path !== null && project.archived_at === null,
  );

  if (picking) {
    return (
      <div className="aw-code-chooser">
        {/* 只有在还有别的项目可选时才给「取消」。一个项目都没有的时候，取消会把
            人送回一个同样只能选文件夹的屏幕——一个什么也不改变的按钮。
            用属性展开而不是传 `undefined`：`exactOptionalPropertyTypes` 下
            「不传这个属性」和「传了 undefined」是两回事，而这里要的是前者。 */}
        <FolderPicker
          busy={open.isPending}
          onChoose={(path) => open.mutate(path)}
          {...(withFolder.length === 0
            ? {}
            : { onCancel: () => setPicking(false) })}
        />
        {open.isError ? (
          <ErrorNotice message={`这个文件夹打不开：${String(open.error)}`} />
        ) : null}
      </div>
    );
  }

  return (
    <div className="aw-code-chooser">
      <div className="aw-code-chooser-head">
        <h1>在哪个文件夹里编码？</h1>
        <p>
          Agent 读写的就是这个文件夹里的真实文件，产物也留在那里。
          在终端里可以用 <code>agent-cli project use</code> 直接把当前目录变成项目。
        </p>
      </div>

      {projects.isPending ? <LoadingLine label="正在读取项目" /> : null}
      {projects.isError ? (
        <ErrorNotice message={`读不到项目列表：${String(projects.error)}`} />
      ) : null}

      {withFolder.length > 0 ? (
        <ul className="aw-code-chooser-list">
          {withFolder.map((project) => (
            <li key={project.project_id}>
              <button onClick={() => onChoose(project)} type="button">
                <Folder aria-hidden="true" size={15} />
                <span className="aw-code-chooser-name">{project.name}</span>
                {/* 路径也显示出来，因为两个项目完全可以同名——它们是两个不同
                    的文件夹，而名字默认取自文件夹名。 */}
                <code dir="ltr">{project.root_path}</code>
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <button
        className="aw-code-chooser-add"
        onClick={() => setPicking(true)}
        type="button"
      >
        <FolderPlus aria-hidden="true" size={15} />
        <span>{withFolder.length === 0 ? "选择一个文件夹" : "选择另一个文件夹"}</span>
      </button>
    </div>
  );
}
