// 对着 mock 数据截三张控制台截图，给 README 与 docs 用。
//
// 不对着真实 API 截：真实 API 里是某个人自己的会话与任务，而 README 是公开的。
// 数据形状抄自 e2e/shell.spec.ts 与各页测试的夹具；页面 1440×900、浅色主题。
//
// 用法（先起预览服务）：
//   ./node_modules/.bin/vite preview --port 4173
//   node scripts/capture-console.mjs
import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const BASE = process.env.CONSOLE_BASE ?? "http://127.0.0.1:4173/ui/";
const OUT = path.resolve(process.cwd(), "../docs/assets/console");

const NOW = "2026-09-05T02:00:00Z";
const fixtures = [
  [/\/v1\/knowledge-bases$/, { knowledge_bases: [{
    knowledge_base_id: "kb_portfolio", name: "校招项目资料", description: "架构、RAG 评测与面试材料",
    can_write: true, document_count: 3, ready_document_count: 3, processing_document_count: 0,
    failed_document_count: 0, created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-02T00:00:00Z",
  }] }],
  [/\/v1\/chat\/sessions$/, { sessions: [
    { session_id: "ses_1", title: "梳理项目资料的三个结论", last_activity_at: NOW, project_id: null },
    { session_id: "ses_2", title: "RAG 评测报告里的引用精度怎么算", last_activity_at: "2026-09-04T12:00:00Z", project_id: null },
    { session_id: "ses_3", title: "比较两种恢复策略", last_activity_at: "2026-09-03T09:00:00Z", project_id: null },
  ] }],
  [/\/v1\/tasks\/capabilities$/, { delegation: {
    enabled: true, max_delegation_depth: 1, max_children_per_run: 6,
    max_parallel_child_invocations: 2, max_tokens_per_agent_invocation: 120000,
  } }],
  [/\/v1\/tasks(\?.*)?$/, { tasks: [
    { task_id: "task_report", status: "waiting_approval", status_detail: null, objective_preview: "比较三个方案并输出一份建议报告", created_at: "2026-09-05T01:40:00Z", updated_at: NOW, agent_invocation_count: 2 },
    { task_id: "task_notes", status: "succeeded", status_detail: null, objective_preview: "整理这批资料，提炼关键结论", created_at: "2026-09-04T10:00:00Z", updated_at: "2026-09-04T10:06:00Z", agent_invocation_count: 1 },
  ], cursor: null }],
  [/\/v1\/approvals(\?.*)?$/, { approvals: [
    { approval_id: "apr_1", task_id: "task_report", status: "pending", decision_version: 1, decided_at: null, created_at: NOW },
  ], cursor: null }],
  [/\/v1\/projects(\?.*)?$/, { projects: [{ project_id: "prj_demo", name: "demo", created_at: "2026-08-22T00:00:00Z", updated_at: "2026-08-22T00:00:00Z", archived_at: null, root_path: "/Users/someone/demo" }] }],
  [/\/health\/live$/, { status: "live" }],
  [/\/health\/ready$/, { status: "ready" }],
  [/\/v1\/settings\/provider-key$/, { active: true, stored: true, fingerprint: "a1b2", path: "~/.config/agent-workbench/key", restart_required: false, restart_hint: "" }],
  [/\/v1\/system\/capabilities$/, { capabilities: [
    { id: "chat.direct", title: "直接对话", tier: "core", state: "available", reason: "", remedy: "", detail: [], provision: "key", switch: null },
    { id: "chat.knowledge_base", title: "知识库问答（RAG）", tier: "core", state: "available", reason: "", remedy: "", detail: [], provision: "install", switch: null },
    { id: "task.submit", title: "任务提交", tier: "core", state: "available", reason: "", remedy: "", detail: [], provision: "none", switch: null },
    { id: "task.worker", title: "任务 Worker", tier: "core", state: "unknown", reason: "这份清单只答得出这个 API 进程装配了什么；Worker 自己登记的在线与否在上面那两格。", remedy: "", detail: [], provision: "none", switch: null },
    { id: "task.mcp", title: "Task 的 MCP 工具", tier: "optional", state: "available", reason: "", remedy: "", detail: ["mcp_word_write_docx", "mcp_web_fetch_page"], provision: "install", switch: null },
    { id: "code.sandbox", title: "编码会话的沙箱", tier: "optional", state: "available", reason: "", remedy: "", detail: [], provision: "install", switch: null },
  ] }],
  [/\/v1\/system\/workers$/, { available: true, observed_at: NOW, workers: [
    { worker_id: "worker_task_01", kind: "task", deployment: "demo-local", capabilities: { demo: false, tools: ["export_artifact", "knowledge_search", "mcp_word_write_docx"] }, started_at: "2026-09-05T01:00:00Z", heartbeat_at: NOW, expires_at: "2026-09-05T02:01:00Z", fresh: true, seconds_since_heartbeat: 4 },
    { worker_id: "worker_ingest_01", kind: "ingestion", deployment: "demo-local", capabilities: { demo: false, sparse: true }, started_at: "2026-09-05T01:00:00Z", heartbeat_at: NOW, expires_at: "2026-09-05T02:01:00Z", fresh: true, seconds_since_heartbeat: 6 },
  ] }],
];

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light", deviceScaleFactor: 1 });
const page = await context.newPage();
await page.addInitScript("window.localStorage.clear()");
await page.route(/\/(v1|health)\//, async (route) => {
  const url = route.request().url();
  const hit = fixtures.find(([pattern]) => pattern.test(url));
  if (hit === undefined || route.request().method() !== "GET") {
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "not mocked" }) });
    return;
  }
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(hit[1]) });
});

await mkdir(OUT, { recursive: true });
for (const [hash, file] of [["#/chat", "chat-start.png"], ["#/work", "task-start.png"], ["#/system", "system-readiness.png"]]) {
  await page.goto(`${BASE}${hash}`);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, file), fullPage: false });
  console.log("captured", file);
}
await browser.close();
