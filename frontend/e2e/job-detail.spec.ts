import { expect, test } from '@playwright/test';

test('任务详情页展示事件区块（历史任务只读样本）', async ({ page }) => {
  await page.goto('/jobs/w');

  await expect(page.getByRole('heading', { level: 2 })).toContainText('w');
  await expect(page.getByText('提交参数', { exact: true })).toBeVisible();

  // “事件”卡片来自独立接口，即使没有事件也必须渲染出区块
  const eventsCard = page.locator('.ant-card').filter({
    has: page.locator('.ant-card-head-title', { hasText: /^事件$/ }),
  });
  await expect(eventsCard).toBeVisible();
  await expect(eventsCard).toContainText(/条|暂无事件/);

  // 日志区块也应在详情页可见
  await expect(page.locator('.ant-card-head-title', { hasText: /^训练日志$/ })).toBeVisible();
});