export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (days > 0) return `${days}天 ${hours}时 ${minutes}分`;
  if (hours > 0) return `${hours}时 ${minutes}分 ${rest}秒`;
  if (minutes > 0) return `${minutes}分 ${rest}秒`;
  return `${rest}秒`;
}

export function cloneName(name: string): string {
  const base = name.slice(0, 45).replace(/-+$/g, '');
  return `${base}-copy`;
}

export function percent(used: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.round((used / total) * 100));
}

/**
 * 排队预估等待：返回 { text, approximate }。
 * approximate=true 表示后端没有历史数据、走的是默认假设，文案要带「约」而不是确定倒计时。
 */
export function formatQueueEta(seconds: number | null | undefined, source?: string | null): {
  text: string;
  approximate: boolean;
} {
  if (seconds === null || seconds === undefined) return { text: '暂无法估算', approximate: true };
  const approximate = !source || source === 'default';
  if (seconds < 60) return { text: '不到 1 分钟', approximate };
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return { text: `${approximate ? '约 ' : ''}${minutes} 分钟`, approximate };
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return { text: `${approximate ? '约 ' : ''}${hours} 小时${rest ? ` ${rest} 分钟` : ''}`, approximate };
}
