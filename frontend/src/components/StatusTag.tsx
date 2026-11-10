import { Tag } from 'antd';
import type { JobStatus } from '../types/api';

const STATUS_META: Record<JobStatus, { label: string; color: string }> = {
  QUEUED: { label: '排队中', color: 'gold' },
  // 和 FAILED 一样用 error 红：都需要人去看一眼，不能等它自己好。
  STARTING_FAILED: { label: '启动失败', color: 'error' },
  RUNNING: { label: '运行中', color: 'processing' },
  SUCCEEDED: { label: '已成功', color: 'success' },
  FAILED: { label: '已失败', color: 'error' },
  CANCELLED: { label: '已取消', color: 'default' },
  UNKNOWN: { label: '状态未知', color: 'purple' },
};

export function StatusTag({ status }: { status: JobStatus | string }) {
  const meta = STATUS_META[status as JobStatus] || STATUS_META.UNKNOWN;
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function PriorityTag({ priority }: { priority: string }) {
  const labels: Record<string, string> = { low: '低', normal: '普通', high: '高' };
  const colors: Record<string, string> = { low: 'default', normal: 'blue', high: 'orange' };
  return <Tag color={colors[priority] || 'default'}>{labels[priority] || priority}</Tag>;
}
