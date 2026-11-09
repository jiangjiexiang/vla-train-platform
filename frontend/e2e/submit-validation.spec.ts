import { expect, test } from '@playwright/test';

test.describe('提交页表单校验', () => {
  test('任务名为空时提交被拦截，且取消宽限期字段存在', async ({ page }) => {
    await page.goto('/submit');
    await expect(page.locator('#name')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { level: 2, name: '提交训练任务' })).toBeVisible();

    // 任务名称下方的取消宽限期字段
    const graceInput = page.getByLabel('取消宽限期（秒）');
    await expect(graceInput).toBeVisible();
    await expect(graceInput).toHaveValue('30');

    await page.locator('#name').fill('');
    await page.getByRole('button', { name: /提交训练任务/ }).click();

    await expect(page.getByText('请输入任务名称')).toBeVisible();
    await expect(page).toHaveURL(/\/submit$/);
  });

  test('大写任务名时提交被拦截', async ({ page }) => {
    await page.goto('/submit');
    await expect(page.locator('#name')).toBeVisible({ timeout: 15_000 });

    await page.locator('#name').fill('E2E-UPPERCASE');
    await page.getByRole('button', { name: /提交训练任务/ }).click();

    await expect(
      page.getByText('仅允许小写字母、数字和中划线，且首尾不能是中划线'),
    ).toBeVisible();
    await expect(page).toHaveURL(/\/submit$/);
  });
});