/**
 * 项目：一件事，以及为它做过的东西。
 *
 * 这一页刻意不是第五个产品。侧栏那一列列的是**项目**，主区列的是这个项目底下
 * 的对话、编码会话、任务和知识库——每一行点开都跳到它自己的产品页去，因为它们
 * 各自的接口才答得准（ADR-071）。
 *
 * 界面上有三件事和数据模型是一一对应的，都不是可选的：
 *
 * * **没有归属是正常状态。** 这一页不会催任何人先建一个项目，也不显示「未归类」
 *   之类的伪分组——不属于任何项目的东西就在它自己的产品页上，那里本来就有。
 * * **删除项目不删除内容。** 确认文案照实说这件事，因为「删除」这个词本身会让人
 *   以为里面的东西也没了。
 * * **归档不是删除。** 归档的项目从列表收起来，深链照样打得开。
 */

import {
  Archive,
  ArchiveRestore,
  FolderOpen,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  createProject,
  deleteProject,
  getProjectItems,
  listProjects,
  renameProject,
  setProjectArchived,
} from "../../api/client";
import type { ProjectItemView, ProjectView } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarActions,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  LoadingLine,
  NewSessionAction,
  formatTime,
} from "../../components/ui";

/** 一件东西点开之后去哪。项目不替它自己的产品页回答任何问题。 */
const ITEM_ROUTES: Readonly<Record<ProjectItemView["kind"], string>> = {
  chat: "/chat",
  code: "/code",
  task: "/work",
  knowledge_base: "/knowledge",
};

const ITEM_LABELS: Readonly<Record<ProjectItemView["kind"], string>> = {
  chat: "对话",
  code: "编码",
  task: "任务",
  knowledge_base: "知识库",
};

export function ProjectsPage() {
  const { identity } = useIdentity();
  const { projectId } = useParams<{ projectId?: string }>();
  const navigate = useNavigate();
  const queries = useQueryClient();
  const workspaceSidebar = useWorkspaceSidebar();

  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);

  const projects = useQuery({
    queryKey: ["projects", identity],
    queryFn: ({ signal }) => listProjects(identity, { signal }),
  });
  const items = useQuery({
    queryKey: ["project-items", identity, projectId],
    enabled: projectId !== undefined,
    queryFn: ({ signal }) => {
      if (projectId === undefined) throw new Error("缺少项目 ID");
      return getProjectItems(identity, projectId, signal);
    },
  });

  const refresh = useCallback(async () => {
    await queries.invalidateQueries({ queryKey: ["projects", identity] });
    await queries.invalidateQueries({ queryKey: ["project-items", identity] });
  }, [identity, queries]);

  const create = useCallback(
    async (name: string) => {
      const trimmed = name.trim();
      if (trimmed === "") {
        setCreating(false);
        return;
      }
      setFailure(null);
      try {
        const created = await createProject(identity, trimmed);
        setCreating(false);
        await refresh();
        await navigate(`/projects/${encodeURIComponent(created.project_id)}`);
      } catch (cause: unknown) {
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [identity, navigate, refresh],
  );

  const rename = useCallback(
    async (target: string, name: string) => {
      const trimmed = name.trim();
      setRenaming(null);
      if (trimmed === "") return;
      setFailure(null);
      try {
        await renameProject(identity, target, trimmed);
        await refresh();
      } catch (cause: unknown) {
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [identity, refresh],
  );

  const archive = useCallback(
    async (target: string, archived: boolean) => {
      setFailure(null);
      try {
        await setProjectArchived(identity, target, archived);
        await refresh();
      } catch (cause: unknown) {
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [identity, refresh],
  );

  const remove = useCallback(
    async (target: string, name: string) => {
      // 文案照实说：删的是这层归属，不是里面的东西。「删除项目」这四个字本身
      // 会让人以为对话和任务也一起没了，而它们不会。
      if (
        !window.confirm(
          `删除项目「${name}」？里面的对话、任务、编码会话和知识库都会留下，只是不再属于任何项目。`,
        )
      ) {
        return;
      }
      setFailure(null);
      try {
        await deleteProject(identity, target);
        await refresh();
        if (target === projectId) await navigate("/projects");
      } catch (cause: unknown) {
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [identity, navigate, projectId, refresh],
  );

  const selected = projects.data?.projects.find(
    (project) => project.project_id === projectId,
  );

  return (
    <div className="aw-projects-page">
      <WorkspaceSidebarActions>
        <NewSessionAction label="新建项目" onClick={() => setCreating(true)} />
      </WorkspaceSidebarActions>
      <WorkspaceSidebarPortal>
        <aside className="aw-projects-sidebar" aria-label="项目列表">
          <IconButton
          className="aw-projects-close"
          label="关闭项目列表"
          onClick={workspaceSidebar.close}
          >
          <X aria-hidden="true" size={17} />
          </IconButton>

          {creating ? (
            <form
              className="aw-session-inline-rename"
              onSubmit={(event) => {
                event.preventDefault();
                const field = new FormData(event.currentTarget).get("name");
                void create(typeof field === "string" ? field : "");
              }}
            >
              <label className="aw-sr-only" htmlFor="aw-project-new">
                项目名字
              </label>
              <input
                autoFocus
                id="aw-project-new"
                name="name"
                onBlur={() => setCreating(false)}
                onKeyDown={(event) => {
                  if (event.key !== "Escape") return;
                  event.preventDefault();
                  setCreating(false);
                }}
                placeholder="这件事叫什么"
              />
            </form>
          ) : null}

          <div className="aw-project-list">
            {projects.isPending ? (
              <LoadingLine label="正在读取项目" />
            ) : projects.data?.projects.length === 0 ? (
              // 空状态说下一步，不说机制，也不催人建项目。
              <p className="aw-chat-local-note">
                把同一件事的对话、任务、编码会话和资料放到一起，就从这里开始。
              </p>
            ) : (
              projects.data?.projects.map((project) => (
                <ProjectRow
                  key={project.project_id}
                  project={project}
                  current={project.project_id === projectId}
                  renaming={renaming === project.project_id}
                  onBeginRename={() => setRenaming(project.project_id)}
                  onCancelRename={() => setRenaming(null)}
                  onRename={(name) => void rename(project.project_id, name)}
                  onArchive={() =>
                    void archive(
                      project.project_id,
                      project.archived_at === null,
                    )
                  }
                  onDelete={() => void remove(project.project_id, project.name)}
                  onOpen={workspaceSidebar.close}
                  inputRef={renameInputRef}
                />
              ))
            )}
          </div>
        </aside>
      </WorkspaceSidebarPortal>

      <main className="aw-projects-main">
        {failure === null ? null : <ErrorNotice message={failure} />}
        {projectId === undefined ? (
          <EmptyState
            icon={<FolderOpen aria-hidden="true" size={24} />}
            title="选一个项目"
            description="项目把同一件事的对话、任务、编码会话和资料收在一起。不属于任何项目的东西仍然在它自己的页面上。"
          />
        ) : (
          <>
            <header className="aw-projects-header">
              <h1>{selected?.name ?? "项目"}</h1>
              {selected?.archived_at === null ||
              selected === undefined ? null : (
                <small>已归档</small>
              )}
            </header>
            {items.isPending ? (
              <LoadingLine label="正在读取这个项目里的东西" />
            ) : items.data?.items.length === 0 ? (
              <EmptyState
                icon={<FolderOpen aria-hidden="true" size={24} />}
                title="这个项目还是空的"
                description="在对话、任务、编码会话或知识库里把一件东西归到这个项目，它就会出现在这里。"
              />
            ) : (
              <ul className="aw-project-items">
                {items.data?.items.map((item) => (
                  <li key={`${item.kind}:${item.item_id}`}>
                    <Link
                      className="aw-project-item"
                      to={`${ITEM_ROUTES[item.kind]}/${encodeURIComponent(item.item_id)}`}
                    >
                      <span className="aw-project-item-kind">
                        {ITEM_LABELS[item.kind]}
                      </span>
                      <span className="aw-project-item-title">
                        {item.title ?? "还没有名字"}
                      </span>
                      <time dateTime={item.ordered_at}>
                        {formatTime(item.ordered_at)}
                      </time>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </main>
    </div>
  );
}

function ProjectRow({
  project,
  current,
  renaming,
  onBeginRename,
  onCancelRename,
  onRename,
  onArchive,
  onDelete,
  onOpen,
  inputRef,
}: {
  project: ProjectView;
  current: boolean;
  renaming: boolean;
  onBeginRename: () => void;
  onCancelRename: () => void;
  onRename: (name: string) => void;
  onArchive: () => void;
  onDelete: () => void;
  onOpen: () => void;
  inputRef: React.RefObject<HTMLInputElement | null>;
}) {
  if (renaming) {
    return (
      <div className="aw-project-row">
        <form
          className="aw-session-inline-rename"
          onSubmit={(event) => {
            event.preventDefault();
            const field = new FormData(event.currentTarget).get("name");
            onRename(typeof field === "string" ? field : "");
          }}
        >
          <label
            className="aw-sr-only"
            htmlFor={`aw-project-rename-${project.project_id}`}
          >
            项目名字
          </label>
          <input
            autoFocus
            defaultValue={project.name}
            id={`aw-project-rename-${project.project_id}`}
            name="name"
            onBlur={onCancelRename}
            onFocus={(event) => event.currentTarget.select()}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              event.stopPropagation();
              onCancelRename();
            }}
            ref={inputRef}
          />
        </form>
      </div>
    );
  }
  return (
    <div className="aw-project-row">
      <Link
        aria-current={current ? "page" : undefined}
        className={`aw-project-link ${current ? "is-active" : ""}`}
        onClick={onOpen}
        onKeyDown={(event) => {
          if (event.key !== "F2") return;
          event.preventDefault();
          onBeginRename();
        }}
        to={`/projects/${encodeURIComponent(project.project_id)}`}
      >
        <span className="aw-project-name">{project.name}</span>
        {project.archived_at === null ? null : <small>已归档</small>}
      </Link>
      <span className="aw-session-row-actions">
        <button
          aria-label={`重命名项目 ${project.name}`}
          className="aw-project-rename"
          onClick={onBeginRename}
          title="重命名"
          type="button"
        >
          <Pencil aria-hidden size={12} />
        </button>
        <button
          aria-label={
            project.archived_at === null
              ? `归档项目 ${project.name}`
              : `取消归档 ${project.name}`
          }
          className="aw-project-archive"
          onClick={onArchive}
          title={project.archived_at === null ? "归档" : "取消归档"}
          type="button"
        >
          {project.archived_at === null ? (
            <Archive aria-hidden size={12} />
          ) : (
            <ArchiveRestore aria-hidden size={12} />
          )}
        </button>
        <button
          aria-label={`删除项目 ${project.name}`}
          className="aw-project-delete"
          onClick={onDelete}
          title="删除"
          type="button"
        >
          <Trash2 aria-hidden size={13} />
        </button>
      </span>
    </div>
  );
}
