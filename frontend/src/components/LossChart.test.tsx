import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { MetricPoint } from '../types/api';
import { LossChart, MAX_RENDER_POINTS, buildSeries, downsample } from './LossChart';

function points(count: number): MetricPoint[] {
  return Array.from({ length: count }, (_, index) => ({ step: index + 1, loss: 1 / (index + 1) }));
}

describe('downsample', () => {
  it('点数不超过上限时原样返回', () => {
    const input = points(50);
    expect(downsample(input)).toBe(input);
  });

  it('超过上限时均匀取样并保留首尾点', () => {
    const input = points(1000);
    const sampled = downsample(input);
    expect(sampled).toHaveLength(MAX_RENDER_POINTS);
    expect(sampled[0]).toEqual(input[0]);
    expect(sampled[sampled.length - 1]).toEqual(input[input.length - 1]);
  });
});

describe('buildSeries', () => {
  it('空数据返回 null', () => {
    expect(buildSeries([])).toBeNull();
  });

  it('全部 loss 相同时也有跨度，不除零', () => {
    const geometry = buildSeries([
      { step: 1, loss: 0.5 },
      { step: 2, loss: 0.5 },
    ]);
    expect(geometry).not.toBeNull();
    expect(geometry!.maxY).toBeGreaterThan(geometry!.minY);
  });

  it('单个点也能生成合法 path', () => {
    const geometry = buildSeries([{ step: 7, loss: 0.3 }]);
    expect(geometry!.path).toMatch(/^M[0-9.]+,[0-9.]+$/);
  });
});

describe('LossChart', () => {
  it('没有数据时显示占位文案', () => {
    render(<LossChart points={[]} />);
    expect(screen.getByTestId('loss-chart-empty')).toBeInTheDocument();
    expect(screen.getByText('暂无训练指标（loss）')).toBeInTheDocument();
  });

  it('有数据时画折线，并降采样到上限以内', () => {
    const { container } = render(<LossChart points={points(500)} />);
    expect(screen.getByTestId('loss-chart')).toBeInTheDocument();
    const path = container.querySelector('.loss-chart-line');
    expect(path?.getAttribute('d')?.split('L')).toHaveLength(MAX_RENDER_POINTS);
    expect(screen.getByText(/200 个点/)).toBeInTheDocument();
  });

  it('鼠标移动时显示最近点的数值', () => {
    const { container } = render(<LossChart points={points(10)} />);
    const svg = container.querySelector('svg') as SVGSVGElement;
    // jsdom 不做布局：clientWidth 为 0，按 viewBox 宽度模拟一个容器宽度
    Object.defineProperty(svg, 'clientWidth', { configurable: true, value: 720 });
    fireEvent.mouseMove(svg, { clientX: 400 });
    expect(screen.getByText(/step \d+ · loss/)).toBeInTheDocument();
    fireEvent.mouseLeave(svg);
    expect(screen.getByText(/step 1 → 10 · 10 个点/)).toBeInTheDocument();
  });

  it('缺少 loss 字段的点被跳过', () => {
    render(<LossChart points={[{ step: 1 }, { step: 2, loss: 0.4 }]} />);
    expect(screen.getByTestId('loss-chart')).toBeInTheDocument();
    expect(screen.getByText(/1 个点/)).toBeInTheDocument();
  });
});