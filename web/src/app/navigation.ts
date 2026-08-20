import {
  Activity,
  Code2,
  FlaskConical,
  Library,
  ListTodo,
  MessageSquare,
  MonitorSmartphone,
  type LucideIcon,
} from "lucide-react";

export interface NavigationItem {
  to: string;
  label: string;
  description: string;
  icon: LucideIcon;
  primary: boolean;
  covers: readonly string[];
}

export interface QuickDestination {
  to: string;
  label: string;
  group: string;
  description: string;
  keywords: string;
  icon: LucideIcon;
}

/** Match a route root or one of its descendants, never a lookalike prefix. */
export function isPathWithin(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export const NAVIGATION = [
  {
    to: "/chat",
    label: "对话",
    description: "直接提问，或依据知识库回答",
    icon: MessageSquare,
    primary: true,
    covers: ["/chat"],
  },
  {
    to: "/work",
    label: "任务",
    description: "提交并追踪可恢复的自动化工作流",
    icon: ListTodo,
    primary: true,
    covers: ["/work"],
  },
  {
    to: "/code",
    label: "Code",
    description: "带工作区与文件预览的编码会话",
    icon: Code2,
    primary: true,
    covers: ["/code"],
  },
  {
    to: "/knowledge",
    label: "知识库",
    description: "管理资料、索引状态与上传",
    icon: Library,
    primary: false,
    covers: ["/knowledge"],
  },
  {
    to: "/evaluation",
    label: "效果评测",
    description: "查看和运行可复现的评测",
    icon: FlaskConical,
    primary: false,
    covers: ["/evaluation"],
  },
  {
    to: "/computer",
    label: "计算机",
    description: "了解屏幕控制的安全边界",
    icon: MonitorSmartphone,
    primary: false,
    covers: ["/computer"],
  },
  {
    to: "/system",
    label: "运行状态",
    description: "检查 API、数据库与本地身份",
    icon: Activity,
    primary: false,
    covers: ["/system"],
  },
] as const satisfies readonly NavigationItem[];

export const QUICK_DESTINATIONS = [
  ...NAVIGATION.map((item) => ({
    to: item.to,
    label: item.label,
    group: item.primary ? "工作" : "资源与工具",
    description: item.description,
    keywords:
      item.to === "/chat"
        ? "chat 聊天 问答 会话"
        : item.to === "/work"
          ? "work task 工作流 任务 时间线"
          : item.to === "/code"
            ? "coding 编码 文件 工作区"
            : item.to === "/knowledge"
              ? "knowledge rag 文档 资料 上传"
              : item.to === "/evaluation"
                ? "eval benchmark 评测 报告"
                : item.to === "/computer"
                  ? "computer screen 屏幕 权限 安全"
                  : "system health status 健康 服务 身份",
    icon: item.icon,
  })),
] as const satisfies readonly QuickDestination[];
