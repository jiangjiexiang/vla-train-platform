import { expect, test } from '@playwright/test';

test('总览页展示 GPU 卡片、实时使用情况和节点/任务统计', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { level: 2, name: '集群总览' })).toBeVisible();

  // 顶部三张 GPU 汇总卡片，数值必须已经从 API 读到（不是占位符 —）
  for (const title of ['GPU 总数', '已使用 GPU', '空闲 GPU']) {
    const stat = page.locator('.ant-statistic').filter({
      has: page.locator('.ant-statistic-title', { hasText: new RegExp(`^${title}$`) }),
    });
    await expect(stat).toBeVisible();
    await expect(stat.locator('.ant-statistic-content-value')).toHaveText(/^\d+$/);
  }

  // GPU 实时使用情况卡片与具体 GPU 行
  const metricsCard = page.locator('.ant-card').filter({
    has: page.locator('.ant-card-head-title', { hasText: 'GPU 实时使用情况' }),
  });
  await expect(metricsCard).toBeVisible();
  await expect(metricsCard.getByText(/^GPU \d+$/).first()).toBeVisible({ timeout: 15_000 });

  // 任务统计六个状态
  const statsCard = page.locator('.ant-card').filter({
    has: page.locator('.ant-card-head-title', { hasText: '任务统计' }),
  });
  for (const title of ['排队中', '运行中', '已成功', '已失败', '已取消', '状态未知']) {
    await expect(statsCard.locator('.ant-statistic-title', { hasText: new RegExp(`^${title}$`) })).toBeVisible();
  }
  await expect(statsCard.locator('.ant-statistic-content-value')).toHaveCount(6);

  // 节点统计卡片与节点表格
  const nodeCard = page.locator('.ant-card').filter({
    has: page.locator('.ant-card-head-title', { hasText: /^节点 \(\d+\)$/ }),
  });
  await expect(nodeCard).toBeVisible();
  await expect(nodeCard.locator('tbody tr.ant-table-row').first()).toBeVisible({ timeout: 15_000 });
});