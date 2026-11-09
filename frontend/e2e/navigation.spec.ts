import { expect, test } from '@playwright/test';

test('顶部导航在总览、提交、任务、队列页面之间切换正常', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { level: 2, name: '集群总览' })).toBeVisible();

  await page.getByRole('menuitem', { name: '提交任务' }).click();
  await expect(page).toHaveURL(/\/submit$/);
  await expect(page.getByRole('heading', { level: 2, name: '提交训练任务' })).toBeVisible();

  await page.getByRole('menuitem', { name: '训练任务' }).click();
  await expect(page).toHaveURL(/\/jobs$/);
  await expect(page.getByRole('heading', { level: 2, name: '训练任务' })).toBeVisible();

  await page.getByRole('menuitem', { name: '资源队列' }).click();
  await expect(page).toHaveURL(/\/queues$/);
  await expect(page.getByRole('heading', { level: 2, name: '资源队列' })).toBeVisible();

  await page.getByRole('menuitem', { name: '集群总览' }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('heading', { level: 2, name: '集群总览' })).toBeVisible();
});