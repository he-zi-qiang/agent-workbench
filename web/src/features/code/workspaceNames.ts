/**
 * 把一个文件夹里的路径，压成会话工作区收得下的名字。
 *
 * **为什么需要压。** 会话工作区是一张平的表，`domain/workspace.py` 的
 * `WorkspaceName` 是 `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`——字符类里没有 `/` 也没有
 * `\`，而且首字符必须是字母数字，于是 `.` 和 `..` 连名字都不是。那一条不是疏忽，
 * 是买来的性质：那个文件里写着「一个客户端给的路径，正是路径穿越和跨租户读取进入
 * 系统的方式」。目录树在这里是**被拒绝**的，不是还没做。
 *
 * **所以这里做的是一次有损变换，而它必须被说出来。** 上传前那句确认里带着一个真实
 * 的例子（`src/app/main.ts → src-app-main.ts`），因为「名字会变」这件事光靠一句抽象
 * 的说明，读者不会真的想象出结果；而他事后在工作区里找不到 `main.ts` 时，会以为
 * 上传失败了。
 *
 * **重名。** 压完之后 `a/x.ts` 和 `a-x.ts` 会撞在一起。撞了就加后缀 `-2`、`-3`，
 * 而不是让后一个覆盖前一个：工作区的写是 compare-and-set，覆盖不会报错，只会安静地
 * 少一个文件。后缀加在扩展名**之前**，因为扩展名是别的东西（预览、`is_text`、
 * `media_type`）在读的。
 */

/** `WorkspaceName` 的两条硬约束，抄自 `domain/workspace.py`。 */
const MAX_NAME_LENGTH = 128;
const ALLOWED = /[^a-zA-Z0-9._-]/g;

/** 一次能放进工作区的条数，`MAX_WORKSPACE_ENTRIES`。 */
export const MAX_WORKSPACE_ENTRIES = 256;

/**
 * 不跟着上传的那些路径段。
 *
 * 浏览器的目录选择器给的是**整棵树**，没有 `.gitignore` 这一说。一个真实仓库里
 * `node_modules` 与 `.git` 常常是文件数的九成以上，而工作区一共只收 256 条——不滤
 * 掉它们，这个功能对任何一个真实目录都直接撞上限，且撞在一堆没有人想上传的东西上。
 *
 * 滤掉这件事**要说出来**，所以确认那一句里带着被滤掉的条数。一个安静的过滤器会让
 * 「我的 .env 呢」变成一个没有答案的问题。
 */
const SKIPPED_SEGMENTS = new Set(["node_modules", ".git", ".venv", "__pycache__"]);

export interface FlattenedUpload {
  file: File;
  /** 压完、去过重之后，真正 PUT 上去的那个名字。 */
  name: string;
  /** 浏览器给的原路径，用来在界面上说「它本来叫什么」。 */
  path: string;
}

export interface FolderUploadPlan {
  entries: FlattenedUpload[];
  /** 被 `SKIPPED_SEGMENTS` 挡掉的条数。 */
  skipped: number;
  /** 名字撞了、加了后缀的条数。 */
  renamed: number;
}

/** 浏览器给的相对路径；单文件选择时它是空串，退回文件名。 */
function pathOf(file: File): string {
  const relative = (file as File & { webkitRelativePath?: string })
    .webkitRelativePath;
  return relative !== undefined && relative !== "" ? relative : file.name;
}

/**
 * 一条路径压成一个合法名字。
 *
 * 分隔符先变成 `-`（而不是被删掉）：删掉会把 `a/b.ts` 和 `ab.ts` 折成同一个名字，
 * 而那正是 `adapters/mcp/naming.py` 拒绝静默删除字符时给的理由——折叠之后的碰撞
 * 事后解释不清。其余不合法的字符同样换成 `-`，中文文件名因此会变成一串 `-`，所以
 * 全是分隔符的名字兜底成 `file`：一个空名字会被服务端 422，而那时读者已经等了
 * 二十个上传。
 */
export function flattenPath(path: string): string {
  const collapsed = path.replace(/[/\\]+/g, "-").replace(ALLOWED, "-");
  // 首字符必须是字母数字，所以前导的 `.`、`-`、`_` 一律削掉——`.env` 因此是 `env`。
  const trimmed = collapsed.replace(/^[^a-zA-Z0-9]+/, "");
  if (trimmed === "") return "file";
  if (trimmed.length <= MAX_NAME_LENGTH) return trimmed;
  // 超长时保住扩展名而不是从右边一刀切：切掉扩展名的那一版会让预览与 media_type
  // 一起失准，而那是这个名字在界面上唯一还被读的部分。
  const dot = trimmed.lastIndexOf(".");
  const extension = dot > 0 ? trimmed.slice(dot, dot + 16) : "";
  return trimmed.slice(0, MAX_NAME_LENGTH - extension.length) + extension;
}

/** 名字撞了就加 `-2`、`-3`，后缀落在扩展名之前。 */
function deduplicate(name: string, taken: Set<string>): string {
  if (!taken.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const extension = dot > 0 ? name.slice(dot) : "";
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${stem}-${String(index)}${extension}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${stem}-${String(Date.now())}${extension}`;
}

/**
 * 一次文件夹上传会发生什么，在发生之前算出来。
 *
 * 返回一份计划而不是直接开始传：确认那一句要说的三个数（传几个、滤掉几个、改名
 * 几个）都在这里，而它们只有把整份清单走一遍才知道。
 */
export function planFolderUpload(chosen: FileList | null): FolderUploadPlan {
  const entries: FlattenedUpload[] = [];
  const taken = new Set<string>();
  let skipped = 0;
  let renamed = 0;
  for (const file of Array.from(chosen ?? [])) {
    const path = pathOf(file);
    const segments = path.split(/[/\\]/);
    // 最后一段是文件名本身，它叫 `.gitignore` 是完全正常的；被滤的是**目录**。
    if (segments.slice(0, -1).some((segment) => SKIPPED_SEGMENTS.has(segment))) {
      skipped += 1;
      continue;
    }
    const flattened = flattenPath(path);
    const name = deduplicate(flattened, taken);
    if (name !== flattened) renamed += 1;
    taken.add(name);
    entries.push({ file, name, path });
  }
  return { entries, skipped, renamed };
}

/**
 * 上传前给人看的那一句。
 *
 * 带一个**真实的**例子而不是一句抽象说明：读者对「名字会变」的想象，和它压出来的
 * 结果往往不是一回事，而他事后在工作区里找不到 `main.ts` 时会以为上传失败了。
 */
export function describeFolderUpload(plan: FolderUploadPlan): string {
  const first = plan.entries[0];
  const lines = [
    `要往这段会话的工作区里放 ${String(plan.entries.length)} 个文件。`,
    "工作区是一张平的表，没有目录，所以路径会被压进名字里：",
    first === undefined ? "" : `${first.path} → ${first.name}`,
  ];
  if (plan.skipped > 0) {
    lines.push(
      `另有 ${String(plan.skipped)} 个在 node_modules / .git / .venv / __pycache__ 里，不传。`,
    );
  }
  if (plan.renamed > 0) {
    lines.push(`其中 ${String(plan.renamed)} 个压完重名，加了 -2 这样的后缀。`);
  }
  return lines.filter((line) => line !== "").join("\n");
}
