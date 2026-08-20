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
            can_write: true,
            document_count: 1,
            ready_document_count: 1,
            processing_document_count: 0,
            failed_document_count: 0,
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

test("对话、任务与 Code 在桌面和移动布局中均可使用", async ({
  page,
}, testInfo) => {
  await page.goto("/ui/");

  await expect(page).toHaveURL(/#\/chat$/);
  await expect(page.getByRole("heading", { name: "新会话" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "有什么可以帮你？" }),
  ).toBeVisible();
  // The name is the button's aria-label, which says where the file goes rather
  // than what it attaches to: uploads land in a knowledge base and stay there.
  // If this ever fails again, follow AttachmentTray's label — do not rename the
  // button back to a per-message "attachment" this system does not have.
  await expect(
    page.getByRole("button", { name: "上传文件到知识库" }),
  ).toBeVisible();
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

  if (isMobile) {
    await page.getByRole("button", { name: "打开会话列表" }).click();
    const sessions = page.getByRole("complementary", { name: "Chat 会话" });
    await expect(sessions).toBeVisible();
    await sessions.getByRole("button", { name: "关闭会话列表" }).click();
    await expect(sessions).toBeHidden();
  }

  await activeNavigation.getByRole("link", { name: "任务" }).click();

  await expect(page).toHaveURL(/#\/work$/);
  await expect(
    activeNavigation.getByRole("link", { name: "任务" }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "想完成什么？" })).toBeVisible();
  // 「新任务」是侧栏里的一行，和 Chat 的「新对话」同一个位置——桌面上一直在，
  // 手机上要先拉开抽屉。断言入口而不是只断言表单，是因为表单一旦默认收起，
  // 只看控件的测试会连"入口还在不在"都答不出来。
  //
  // `exact` 是必需的，而且是这条断言唯一一次真正失败的原因：Playwright 的
  // `name` 默认按子串匹配，而「刷新任务列表」的无障碍名里正好含有「新任务」
  // 三个字，于是同一个选择器同时命中刷新按钮，报 strict mode violation。
  if (isMobile) {
    await page.getByRole("button", { name: "打开任务列表" }).click();
    const tasks = page.getByRole("complementary", {
      name: "任务列表与新建任务",
    });
    await expect(
      tasks.getByRole("button", { name: "新任务", exact: true }),
    ).toBeVisible();
    await tasks.getByRole("button", { name: "关闭任务列表" }).click();
    await expect(tasks).toBeHidden();
  } else {
    await expect(
      page.getByRole("button", { name: "新任务", exact: true }),
    ).toBeVisible();
  }
  await expect(
    page.getByRole("button", { name: "上传文件到知识库" }),
  ).toBeVisible();
  await expect(page.getByLabel("回答资料")).toHaveValue("");

  // The other primary flow, and the one that is still its own rail entry.
  // Asserted by name and by the empty state rather than by a count: a count
  // would pass for a rail with two of the wrong things on it.
  await activeNavigation.getByRole("link", { name: "Code", exact: true }).click();

  await expect(page).toHaveURL(/#\/code$/);
  await expect(page.getByRole("heading", { name: "开始编码" })).toBeVisible();
  // The composer, on a page with no session yet, is the assertion. This used to
  // wait for a 「新建编码会话」 button, which was a full-screen door whose only
  // effect was a POST the first instruction can carry itself -- so a coding
  // tool asked for a click before it would show an input. The session is opened
  // lazily on the first send now, and the thing worth pinning is that there is
  // nothing to click first.
  await expect(page.getByLabel("要做的事")).toBeVisible();

  // Back to the workbench, and the composer must still be inside the viewport.
  // A tab strip added above a `100dvh` grid pushes it out by exactly its own
  // height, and `toBeVisible()` reports an element that has been pushed off the
  // bottom as visible -- so this is the assertion that catches a botched row.
  await activeNavigation.getByRole("link", { name: "对话" }).click();
  await expect(page.getByLabel("问题", { exact: true })).toBeInViewport();
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
  await expect(page.getByRole("heading", { name: "校招项目资料" })).toBeVisible();
  // 名字里不再出现 Chat：导航把这个产品叫「对话」，这颗按钮此前叫的是别的。
  // 不加 exact 也行——「在对话中使用」是唯一含这一串的可及名字，导航里那个
  // 「对话」比它短，子串匹配只会从长的一侧命中。
  await page.getByRole("link", { name: "在对话中使用" }).click();
  await expect(page).toHaveURL(/#\/chat\?kb=kb_portfolio$/);
  await expect(page.getByLabel("回答资料")).toHaveValue("kb_portfolio");

  await navigation.getByRole("button", { name: "更多" }).click();
  const more = page.getByRole("dialog", { name: "更多页面" });
  await expect(more.getByRole("link", { name: "运行状态" })).toBeVisible();
  await more.getByRole("link", { name: "效果评测" }).click();
  // The page's heading is the question it answers; "效果评测" is the eyebrow
  // above it and the nav label, not a heading. Asserting the heading keeps this
  // checking that navigation landed on the right page rather than that some
  // element somewhere repeats the link text.
  await expect(
    page.getByRole("heading", { name: "找资料，找得准吗？" }),
  ).toBeVisible();
});

test("快速跳转能按用途找到精确页面", async ({ page }, testInfo) => {
  await page.goto("/ui/");
  const isMobile = testInfo.project.name === "mobile";

  if (isMobile) {
    await page.getByRole("button", { name: "更多" }).click();
    const more = page.getByRole("dialog", { name: "更多页面" });
    // “计算机”单看名字像一个实时控制台，说明在点击之前就把它的真实用途说清楚。
    await expect(more.getByText("了解屏幕控制的安全边界")).toBeVisible();
    await more.getByRole("button", { name: "快速跳转" }).click();
  } else {
    // Wait for React to hydrate the shell and register the global shortcut.
    // Pressing immediately after page.goto races the AppShell effect on a
    // cold Vite load even though the static document is already available.
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
    await page.keyboard.press("Control+K");
  }

  const switcher = page.getByRole("dialog", { name: "快速跳转" });
  await expect(switcher).toBeVisible();
  await switcher.getByRole("combobox", { name: "搜索页面" }).fill("可恢复 工作流");
  await expect(switcher.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/#\/work$/);
  await expect(page.getByRole("heading", { name: "想完成什么？" })).toBeVisible();
  await expect(switcher).toBeHidden();
});
