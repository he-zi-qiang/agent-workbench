import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript("window.localStorage.clear()");

  await page.route(/\/v1\/knowledge-bases$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        knowledge_bases: [
          {
            knowledge_base_id: "kb_portfolio",
            name: "校招项目资料",
            description: "架构、RAG 评测与面试材料",
            document_count: 1,
            ready_document_count: 1,
            processing_document_count: 0,
            created_at: "2026-08-01T00:00:00Z",
            updated_at: "2026-08-02T00:00:00Z",
          },
        ],
      }),
    });
  });

  await page.route(/\/v1\/knowledge-bases\/kb_portfolio\/documents$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ documents: [] }),
    });
  });

  await page.route(/\/v1\/tasks(?:\?.*)?$/, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ tasks: [], cursor: null }),
    });
  });
});

test("Chat 与 Work 外壳在桌面和移动布局中均可使用", async ({
  page,
}, testInfo) => {
  await page.goto("/ui/");

  await expect(page).toHaveURL(/#\/chat$/);
  await expect(page.getByRole("heading", { name: "新会话" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "今天想聊什么？" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "添加附件" })).toBeVisible();
  await expect(page.getByLabel("回答资料")).toHaveValue("");

  const isMobile = testInfo.project.name === "mobile";
  const activeNavigation = page.getByRole("navigation", {
    name: isMobile ? "移动端导航" : "主导航",
  });
  const inactiveNavigation = page.getByRole("navigation", {
    name: isMobile ? "主导航" : "移动端导航",
  });

  await expect(activeNavigation).toBeVisible();
  await expect(inactiveNavigation).toBeHidden();

  await activeNavigation.getByRole("link", { name: "Work", exact: true }).click();

  await expect(page).toHaveURL(/#\/work$/);
  await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "新建任务" })).toBeVisible();
  await expect(page.getByRole("button", { name: "添加附件" })).toBeVisible();
  await expect(page.getByLabel("回答资料")).toHaveValue("");
});

test("知识库能进入 Chat，辅助页面在移动端也可到达", async ({
  page,
}, testInfo) => {
  await page.goto("/ui/");
  const isMobile = testInfo.project.name === "mobile";
  const navigation = page.getByRole("navigation", {
    name: isMobile ? "移动端导航" : "主导航",
  });

  await navigation.getByRole("link", { name: "知识库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "知识库", exact: true })).toBeVisible();
  await expect(page.getByText("校招项目资料", { exact: true }).first()).toBeVisible();
  await page.getByRole("link", { name: "在 Chat 中使用" }).click();
  await expect(page).toHaveURL(/#\/chat\?kb=kb_portfolio$/);
  await expect(page.getByLabel("回答资料")).toHaveValue("kb_portfolio");

  if (isMobile) {
    await navigation.getByRole("button", { name: "更多" }).click();
    const more = page.getByRole("dialog", { name: "更多页面" });
    await expect(more.getByRole("link", { name: "待我确认" })).toBeVisible();
    await expect(more.getByRole("link", { name: "运行状态" })).toBeVisible();
    await more.getByRole("link", { name: "效果评测" }).click();
  } else {
    await navigation.getByRole("link", { name: "效果评测" }).click();
  }
  // The page's heading is the question it answers; "效果评测" is the eyebrow
  // above it and the nav label, not a heading. Asserting the heading keeps this
  // checking that navigation landed on the right page rather than that some
  // element somewhere repeats the link text.
  await expect(
    page.getByRole("heading", { name: "找资料，找得准吗？" }),
  ).toBeVisible();
});
