import { describe, expect, it } from 'vitest';

import type { MetricPoint } from '../types/api';
import { metricOptions } from './metricOptions';

describe('metricOptions', () => {
  it('只有 loss 时只给一个指标', () => {
    const items: MetricPoint[] = [{ step: 1, loss: 0.5 }, { step: 2, loss: 0.4 }];

    expect(metricOptions(items)).toEqual([{ label: 'loss', value: 'loss' }]);
  });

  it('真实 SmolVLA 训练会带出 grad_norm / lr / update_s', () => {
    const items: MetricPoint[] = [
      { step: 1, loss: 0.66, grad_norm: 5.87, lr: 9.97e-5, update_s: 0.5 },
      { step: 2, loss: 4.17, grad_norm: 22.3, lr: 9.89e-5, update_s: 0.07 },
    ];

    expect(metricOptions(items).map((o) => o.value)).toEqual(['loss', 'grad_norm', 'lr', 'update_s']);
    expect(metricOptions(items)[1].label).toBe('梯度范数');
  });

  it('loss 永远排第一，不随字段出现顺序变化', () => {
    const items: MetricPoint[] = [{ step: 1, lr: 1e-4, loss: 0.5 }];

    expect(metricOptions(items)[0].value).toBe('loss');
  });

  it('忽略非数值与 NaN/Infinity', () => {
    const items: MetricPoint[] = [
      { step: 1, loss: 0.5, note: 'hello', grad_norm: Number.NaN },
    ];

    expect(metricOptions(items).map((o) => o.value)).toEqual(['loss']);
  });

  it('未知字段不会出现（避免画出无意义的空曲线）', () => {
    const items: MetricPoint[] = [{ step: 1, loss: 0.5, custom_metric: 1 }];

    expect(metricOptions(items).map((o) => o.value)).not.toContain('custom_metric');
  });

  it('空数据返回空数组', () => {
    expect(metricOptions([])).toEqual([]);
  });
});
