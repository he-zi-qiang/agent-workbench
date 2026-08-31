import {
  Activity,
  Code2,
  FlaskConical,
  Library,
  ListTodo,
  MessageSquare,
  MonitorSmartphone,
  Wallet,
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
    label: "Chat",
    description: "直接提问，或依据知识库回答",
    icon: MessageSquare,
    primary: true,
    covers: ["/chat"],
  },
  {
    to: "/work",
    label: "Tasks",
    description: "提交并追踪可恢复的自动化工作流",
    icon: ListTodo,
    primary: true,
    covers: ["/work"],
  },
  {
    to: "/code",
    // 「Code」而不是「编码」——这一条推翻了此前那次改名。当时的理由是
    // 「主导航里唯一的英文，夹在对话和任务中间，是那个异类」，而三个工作区
    // 现在一起改成英文，异类不再存在。它底下那个「最近编码」栏头也一起删了
    // （导航项自己就是这一组的标题），所以那条理由列举的三个中文锚点少了
    // 一个；剩下的「开始编码」「编码会话」在各自页面里没动——那两处不是
    // 导航，读者是在已经进来之后才看到它们的。
    // 路由、目录和 `code` 这个关键词一如既往没动。
    label: "Code",
    description: "带工作区与文件预览的编码会话",
    icon: Code2,
    primary: true,
    covers: ["/code"],
  },
  // 这里曾经是「项目」。它被 ADR-074 收进了 Code：Project 不再是横跨对话/任务/
  // 知识库的一层归属，而就是编码工作区——一个有名字的目录，在 Code 里创建和切换。
  //
  // 上面那条注释（「项目回答这是为哪件事做的」）当时是对的，而它没有兑现：那层
  // 归属除了在两个下拉框里被设置，从未被任何界面用来把三样东西放到一起看。一个
  // 只能被设置、不能被使用的维度，不是维度。
  {
    to: "/knowledge",
    label: "知识库",
    description: "管理资料、索引状态与上传",
    icon: Library,
    primary: false,
    covers: ["/knowledge"],
  },
  {
    to: "/usage",
    label: "用量",
    // 「资源」那一组，不是第四个工作区：它不是一个能在里面干活的地方，是一份
    // 关于另外三个地方的账。
    description: "三个模式各花了多少 token 和钱",
    icon: Wallet,
    primary: false,
    covers: ["/usage"],
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

/**
 * 三个工作区改用英文名之后，中文别名只能靠 keywords 兜住。
 *
 * 快速跳转按 label + description + keywords 一起匹配，而 label 现在是
 * Chat / Tasks / Code——一个习惯输入「任务」的人本来会一无所获。
 *
 * **这里曾经是一条 `item.to === ... ? ... : ...` 的三元链，而它漏掉了
 * `/usage`。** 漏掉的后果不是「用量页没有关键词」——是它落进兜底串，
 * 于是搜「健康」「身份」会命中用量页，搜「钱」「cost」一无所获。
 * 一条兜底分支把「忘了写」和「就该是这一串」变成了同一件事。
 *
 * 改成一张按路由索引的表，键类型是 `NAVIGATION` 里 `to` 的字面量联合：
 * **新增一个导航项而不给它关键词，是一个类型错误，不是一次静默的降级。**
 */
const QUICK_KEYWORDS: Record<(typeof NAVIGATION)[number]["to"], string> = {
  "/chat": "chat 对话 聊天 问答 会话",
  "/work": "work task 工作流 任务 时间线",
  "/code": "coding 编码 文件 工作区",
  "/knowledge": "knowledge rag 文档 资料 上传",
  "/usage": "usage cost token 用量 花费 成本 钱 账",
  "/evaluation": "eval benchmark 评测 报告",
  "/computer": "computer screen 屏幕 权限 安全",
  "/system": "system health status 健康 服务 身份",
};

export const QUICK_DESTINATIONS = [
  ...NAVIGATION.map((item) => ({
    to: item.to,
    label: item.label,
    group: item.primary ? "工作" : "资源与工具",
    description: item.description,
    keywords: QUICK_KEYWORDS[item.to],
    icon: item.icon,
  })),
] as const satisfies readonly QuickDestination[];
