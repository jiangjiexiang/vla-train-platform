import { expect, test } from '@playwright/test';
import { cancelJobIfActive, cleanupRunJobs, e2eJobName } from './helpers';

test.afterAll(async ({ request }) => {
  const cleaned = await cleanupRunJobs(request);
  if (cleaned.length > 0) {
    console.log(`[e2e] afterAll 兜底取消了残留任务: ${cleaned.join(', ')}`);
  }
});

test('提交不存在镜像的任务，列表可见且能从界面取消', async ({ page, request }) => {
  const name = e2eJobName('cancel');

  try {
    await page.goto('/submit');
    await expect(page.locator('#name')).toBeVisible({ timeout: 15_000 });

    // 镜像不存在 -> Pod 无法启动，不会真正占用 GPU
    await page.locator('#name').fill(name);
    await page.locator('#image').fill('nope:doesnotexist');
    await page.locator('#command').fill('true');
    await page.getByLabel('取消宽限期（秒）').fill('1');

    await page.getByRole('button', { name: /提交训练任务/ }).click();

    // 提交成功后跳转到详情页
    await expect(page).toHaveURL(new RegExp(`/jobs/${name}$`), { timeout: 20_000 });
    await expect(page.getByRole('heading', { level: 2 })).toContainText(name);

    // 回到列表，搜索刚创建的任务
    await page.goto('/jobs');
    await page.locator('.job-search input').fill(name);

    const row = page
      .locator('tr.ant-table-row')
      .filter({ has: page.getByRole('link', { name, exact: true }) });
    await expect(row).toBeVisible({ timeout: 20_000 });

    // 镜像拉取失败时任务应停留在排队（或已失败），不会进入运行中
    await expect(row.locator('td').nth(1)).toHaveText(/排队中|已失败/, { timeout: 20_000 });

    // 从界面取消：行内“取消” -> 确认弹窗
    await row.getByRole('button', { name: /取消/ }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(`确认取消任务 ${name}？`);
    await dialog.getByRole('button', { name: '确认取消', exact: true }).click();

    await expect(page.locator('.ant-message')).toContainText(`任务 ${name} 已取消`, { timeout: 15_000 });
    await expect(row.locator('td').nth(1)).toHaveText('已取消', { timeout: 20_000 });
  } finally {
    // 无论断言是否失败，都不留下活跃任务；已取消时该调用是 no-op
    await cancelJobIfActive(request, name);
  }
});