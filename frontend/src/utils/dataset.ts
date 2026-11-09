/** 数据集/配方页共用的展示格式化，全部是纯函数，便于单测。 */

/** 字节数 → 易读体积：916 B / 12.3 KB / 4.5 GB。 */
export function formatBytes(bytes: number | null | undefined): string {
  const value = Number(bytes ?? 0);
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || size >= 100 ? 0 : 1;
  return `${size.toFixed(digits)} ${units[unit]}`;
}

/** 时长：不足 1 分钟显示「45 秒」，其余显示「X 分钟」（超过 1 小时按小时+分钟）。 */
export function formatMinutes(seconds: number | null | undefined): string {
  const total = Number(seconds ?? 0);
  if (!Number.isFinite(total) || total <= 0) return '—';
  if (total < 60) return `${Math.round(total)} 秒`;
  const minutes = total / 60;
  if (minutes < 60) return `${minutes.toFixed(minutes >= 10 ? 0 : 1)} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return `${hours} 小时 ${rest} 分钟`;
}

/** 整数千分位；非有限值返回 '—'。 */
export function formatCount(value: number | null | undefined): string {
  const number = Number(value ?? 0);
  if (!Number.isFinite(number)) return '—';
  return number.toLocaleString('zh-CN');
}

/** 参数默认值的输入框占位文案：无默认值时返回 undefined。 */
export function paramPlaceholder(param: { placeholder?: string; default?: unknown }): string | undefined {
  if (param.placeholder) return param.placeholder;
  if (param.default === null || param.default === undefined || param.default === '') return undefined;
  return String(param.default);
}

/** 参数面板里展示的默认值（对象/数组用 JSON，空值显示「—」）。 */
export function formatParamDefault(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}