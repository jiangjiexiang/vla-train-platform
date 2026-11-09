import { expect, type APIRequestContext } from '@playwright/test';

export const E2E_PREFIX = 'e2e-';
export const E2E_RUN_ID = Date.now().toString(36);

interface JobSummary {
  name: string;
  status: string;
}

/** 生成本次运行唯一的 e2e 任务名（固定 e2e- 前缀，便于识别）。 */
export function e2eJobName(label: string): string {
  return `${E2E_PREFIX}${E2E_RUN_ID}-${label}`;
}

/**
 * 兜底清理：只有任务仍处于排队/运行状态时才调用取消接口。
 * 已取消/已结束的任务不做任何操作，避免把终态改写成 CANCELLED。
 */
export async function cancelJobIfActive(request: APIRequestContext, name: string): Promise<boolean> {
  const response = await request.get(`/api/v1/jobs/${encodeURIComponent(name)}`);
  if (response.status() === 404) return false;
  expect(response.ok(), `读取任务 ${name} 失败`).toBeTruthy();

  const job = (await response.json()) as JobSummary;
  if (job.status !== 'QUEUED' && job.status !== 'RUNNING') return false;

  const cancelled = await request.delete(`/api/v1/jobs/${encodeURIComponent(name)}`);
  expect(cancelled.ok(), `取消任务 ${name} 失败`).toBeTruthy();
  return true;
}

/** 扫描本次运行创建的任务，取消其中仍活跃的任务，返回被清理的任务名。 */
export async function cleanupRunJobs(request: APIRequestContext): Promise<string[]> {
  const query = encodeURIComponent(`${E2E_PREFIX}${E2E_RUN_ID}-`);
  const response = await request.get(`/api/v1/jobs?limit=200&q=${query}`);
  if (!response.ok()) return [];

  const payload = (await response.json()) as { items?: JobSummary[] };
  const cleaned: string[] = [];
  for (const job of payload.items ?? []) {
    if (!job.name.startsWith(`${E2E_PREFIX}${E2E_RUN_ID}-`)) continue;
    if (job.status !== 'QUEUED' && job.status !== 'RUNNING') continue;
    const cancelled = await request.delete(`/api/v1/jobs/${encodeURIComponent(job.name)}`);
    if (cancelled.ok()) cleaned.push(job.name);
  }
  return cleaned;
}