import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript("window.localStorage.clear()");

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
    page.getByRole("heading", { name: "用可核验的资料开始对话" }),
  ).toBeVisible();

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
});
