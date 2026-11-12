import { getErrorMessage } from '../../api/client';
import type { EffectiveQuota, ParamsTemplate, Priority, SubmitJobRequest } from '../../types/api';
import {
  buildCommandFromTemplate,
  builtinTemplate,
  resolveBaseCommand,
} from '../../utils/buildCommand';

export interface KeyValueRow {
  key?: string;
  value?: string;
}

export interface SubmitFormValues {
  name: string;
  image: string;
  gpus: number;
  queue: string;
  priority: Priority;
  command: string;
  env: KeyValueRow[];
  labels: KeyValueRow[];
  max_retries: number;
  termination_grace_period_seconds: number | null;
  params?: Record<string, unknown>;
  multi_node?: boolean;
  nodes?: number | null;
  gpus_per_node?: number | null;
}

// 契约 1.6：labels 的键与值都须匹配该正则，总长 <= 256 字节
export const LABEL_PATTERN = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
export const MAX_LABELS_BYTES = 256;

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

// 把键值行收敛成对象；遇到重复键时返回该键，交由调用方提示
export function rowsToRecord(rows: KeyValueRow[] | undefined): Record<string, string> | string {
  const record: Record<string, string> = {};
  for (const row of rows || []) {
    const key = (row.key ?? '').trim();
    if (!key) continue;
    if (Object.prototype.hasOwnProperty.call(record, key)) return key;
    record[key] = row.value ?? '';
  }
  return record;
}

// 仅用于 byte 计数提示：重复/空行忽略，不参与提交校验
export function rowsToLabels(rows: KeyValueRow[] | undefined): Record<string, string> {
  const result = rowsToRecord(rows);
  return typeof result === 'string' ? {} : result;
}

export function labelsByteLength(labels: Record<string, string>): number {
  return Object.entries(labels).reduce((sum, [key, value]) => sum + utf8Length(key) + utf8Length(value), 0);
}

export function validateLabels(labels: Record<string, string>): string | null {
  for (const [key, value] of Object.entries(labels)) {
    if (!LABEL_PATTERN.test(key) || !LABEL_PATTERN.test(value)) {
      return `标签 ${key} 的键或值格式无效，仅允许小写字母、数字、中划线和点`;
    }
  }
  if (labelsByteLength(labels) > MAX_LABELS_BYTES) {
    return `标签总长度超过 ${MAX_LABELS_BYTES} 字节，请精简后再提交`;
  }
  return null;
}

/**
 * 表单里「训练命令」框此时应视为空：
 * - 用户完全没填 → 用模板 entrypoint
 * - 用户填的内容恰好等于 /config 的 default_command 且镜像提供了 entrypoint
 *   → 仍然用 entrypoint（说明用户没有真正手写命令，别让默认值挡住镜像模板）
 * 其余情况都算用户手填，优先级最高。
 */
export function pickBaseCommand(options: {
  formCommand?: string | null;
  template?: ParamsTemplate | null;
  defaultCommand?: string | null;
}): string {
  const formCommand = (options.formCommand ?? '').trim();
  const entrypoint = (options.template?.entrypoint ?? '').trim();
  const untouched = !formCommand || Boolean(entrypoint) && formCommand === (options.defaultCommand ?? '').trim();
  return resolveBaseCommand({
    userCommand: untouched ? '' : formCommand,
    entrypoint,
    defaultCommand: options.defaultCommand,
  });
}

export function buildSubmitJobPayload(
  values: SubmitFormValues,
  options: {
    paramsTemplate: ParamsTemplate | null;
    defaultCommand?: string;
    multiNodeEnabled: boolean;
  },
): SubmitJobRequest {
  const env = rowsToRecord(values.env);
  const labels = rowsToRecord(values.labels);
  if (typeof env === 'string' || typeof labels === 'string') {
    throw new Error('键值行必须先查重');
  }

  const multiNode = options.multiNodeEnabled && values.multi_node;
  const gpus = multiNode
    ? Math.max(1, Number(values.nodes) || 1) * Math.max(1, Number(values.gpus_per_node) || 1)
    : values.gpus;
  const chosenBase = pickBaseCommand({
    formCommand: values.command,
    template: options.paramsTemplate,
    defaultCommand: options.defaultCommand,
  });
  const currentTemplate = options.paramsTemplate ?? builtinTemplate();
  const payload: SubmitJobRequest = {
    name: values.name.trim(),
    image: values.image.trim(),
    gpus,
    queue: values.queue,
    priority: values.priority,
    command: buildCommandFromTemplate(currentTemplate, values.params ?? {}, chosenBase),
    env,
    max_retries: values.max_retries,
    termination_grace_period_seconds: values.termination_grace_period_seconds ?? 30,
    labels,
  };
  if (options.multiNodeEnabled) {
    payload.nodes = multiNode ? Math.max(1, Number(values.nodes) || 1) : 1;
    if (multiNode) payload.gpus_per_node = Math.max(1, Number(values.gpus_per_node) || 1);
  }
  return payload;
}

// 422 的配额错误：后端 detail 形如 "GPU quota exceeded: 4 used of 4 allowed"
export function isQuotaError(error: unknown, messageText?: string): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  if (status !== 422) return false;
  const text = messageText ?? getErrorMessage(error);
  return /quota exceeded/i.test(text);
}

/**
 * 配额剩余量提示：优先用 /auth/me 返回的实时用量（gpu_in_use / active_jobs）算剩余；
 * 服务端只给上限不给用量时退化为只显示上限，不臆造剩余。
 */
export function describeQuotaHint(quota: EffectiveQuota | null, requestedGpus: number | null): string {
  if (!quota) return '';
  const parts: string[] = [];
  if (quota.max_gpus !== null && quota.max_gpus !== undefined) {
    if (typeof quota.gpu_in_use === 'number') {
      const remaining = Math.max(0, quota.max_gpus - quota.gpu_in_use);
      parts.push(`GPU 配额：已用 ${quota.gpu_in_use} / ${quota.max_gpus} 卡，剩余 ${remaining} 卡`);
    } else {
      parts.push(`GPU 配额上限 ${quota.max_gpus} 卡`);
    }
  }
  if (quota.max_active_jobs !== null && quota.max_active_jobs !== undefined) {
    if (typeof quota.active_jobs === 'number') {
      const remaining = Math.max(0, quota.max_active_jobs - quota.active_jobs);
      parts.push(`活跃任务：${quota.active_jobs} / ${quota.max_active_jobs}，剩余 ${remaining} 个`);
    } else {
      parts.push(`活跃任务上限 ${quota.max_active_jobs} 个`);
    }
  }
  if (!parts.length) return '';
  const exceeded = requestedGpus !== null
    && typeof quota.gpu_in_use === 'number'
    && quota.max_gpus !== null
    && quota.max_gpus !== undefined
    && quota.gpu_in_use + requestedGpus > quota.max_gpus;
  return `${parts.join('；')}${exceeded ? '；本次申请的 GPU 数将超出配额' : ''}`;
}
