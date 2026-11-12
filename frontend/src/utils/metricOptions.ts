import type { MetricPoint } from '../types/api';

const METRIC_LABELS: Record<string, string> = {
  loss: 'loss',
  grad_norm: '梯度范数',
  lr: '学习率',
  update_s: '每步耗时',
  'train/loss': 'loss',
  'train/grad_norm': '梯度范数',
  'train/lr': '学习率',
  'train/update_s': '每步耗时',
};

const METRIC_ORDER = ['loss', 'grad_norm', 'lr', 'update_s', 'train/loss', 'train/grad_norm', 'train/lr', 'train/update_s'];

export function metricOptions(items: MetricPoint[]): Array<{ label: string; value: string }> {
  const keys = new Set<string>();
  for (const point of items) {
    for (const [key, value] of Object.entries(point)) {
      if (key === 'step') continue;
      if (typeof value === 'number' && Number.isFinite(value)) keys.add(key);
    }
  }
  return METRIC_ORDER
    .filter((key) => keys.has(key))
    .map((key) => ({ label: METRIC_LABELS[key] ?? key, value: key }));
}
