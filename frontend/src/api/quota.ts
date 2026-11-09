import { api } from './client';
import type { EffectiveQuota, UserQuota } from '../types/api';

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

// 取当前用户配额；任何失败（/auth/me 未实现、未登录、网络错误）都视为「没有配额信息」
function safeMe(signal?: AbortSignal): Promise<Record<string, unknown> | null> {
  if (typeof api.me !== 'function') return Promise.resolve(null);
  try {
    return Promise.resolve(api.me(signal)).then((me) => (isObject(me) ? me : null), () => null);
  } catch {
    return Promise.resolve(null);
  }
}

export async function loadQuota(signal?: AbortSignal): Promise<UserQuota | null> {
  const me = await safeMe(signal);
  return isObject(me?.quotas) ? (me.quotas as unknown as UserQuota) : null;
}

// /auth/me 的 quotas 额外带实时用量（gpu_in_use / active_jobs），提交页据此展示剩余量
export async function loadEffectiveQuota(signal?: AbortSignal): Promise<EffectiveQuota | null> {
  const me = await safeMe(signal);
  return isObject(me?.quotas) ? (me.quotas as unknown as EffectiveQuota) : null;
}
