import { expect, test } from '@playwright/test';

test('任务列表渲染已有任务，状态筛选、搜索与分页可用', async ({ page }) => {
  await page.goto('/jobs');
  await expect(page.getByRole('heading', { level: 2, name: '训练任务' })).toBeVisible();

  const rows = page.locator('tr.ant-table-row');
  await expect(rows.first()).toBeVisible({ timeout: 15_000 });
  expect(await rows.count()).toBeGreaterThan(0);

  // 分页控件与总数
  await expect(page.locator('.ant-pagination')).toBeVisible();
  await expect(page.locator('.ant-pagination-total-text')).toContainText(/共 \d+ 个任务/);
  await expect(page.locator('.ant-pagination-options')).toBeVisible();

  const firstJobName = (await rows.first().locator('td').first().innerText()).trim();
  expect(firstJobName.length).toBeGreaterThan(0);

  // 状态筛选：选择“已成功”后所有可见行都应是该状态
  await page.locator('.status-filter .ant-select-selector').click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option[title="已成功"]').click();
  await expect(page.locator('.status-filter')).toContainText('已成功');
  await expect
    .poll(
      async () => {
        const labels = await page.locator('tr.ant-table-row td:nth-child(2)').allInnerTexts();
        return labels.length > 0 && labels.every((label) => label.trim() === '已成功');
      },
      { timeout: 15_000 },
    )
    .toBe(true);

  // 恢复全部状态
  await page.locator('.status-filter .ant-select-selector').click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option[title="全部状态"]').click();
  await expect(page.locator('.status-filter')).toContainText('全部状态');

  // 搜索：按第一行任务名搜索，结果行都必须命中该关键字
  await page.locator('.job-search input').fill(firstJobName);
  const matchedRow = page
    .locator('tr.ant-table-row')
    .filter({ has: page.getByRole('link', { name: firstJobName, exact: true }) });
  await expect(matchedRow).toBeVisible({ timeout: 15_000 });
  await expect
    .poll(
      async () => {
        const names = await page.locator('tr.ant-table-row td:first-child').allInnerTexts();
        const needle = firstJobName.toLowerCase();
        return names.length > 0 && names.every((name) => name.trim().toLowerCase().includes(needle));
      },
      { timeout: 15_000 },
    )
    .toBe(true);
});