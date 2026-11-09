import { Empty, Typography } from 'antd';
import { useMemo, useState } from 'react';

import type { MetricPoint } from '../types/api';

/** 超过这个点数就降采样，避免一张图里塞几千个 path 段。 */
export const MAX_RENDER_POINTS = 200;

/** 图表内部坐标系（viewBox），配合 preserveAspectRatio 自适应容器宽度。 */
const VIEW_WIDTH = 720;
const VIEW_HEIGHT = 260;
const PADDING = { top: 16, right: 20, bottom: 36, left: 64 };

export interface SeriesPoint {
  x: number;
  y: number;
}

/**
 * 均匀降采样：每 N 个点取一个，并始终保留首尾点。
 * 不引入依赖，也不做曲线平滑——训练 loss 的尖峰本身就是要看的信息。
 */
export function downsample<T>(points: T[], maxPoints = MAX_RENDER_POINTS): T[] {
  if (points.length <= maxPoints || maxPoints < 3) return points;
  const step = (points.length - 1) / (maxPoints - 1);
  const sampled: T[] = [];
  for (let index = 0; index < maxPoints; index += 1) {
    sampled.push(points[Math.round(index * step)]);
  }
  return sampled;
}

/** 把 step/loss 点对转成绘图坐标；loss 越小越好，Y 轴反向。 */
export function buildSeries(
  points: Array<{ step: number; loss: number }>,
  width = VIEW_WIDTH,
  height = VIEW_HEIGHT,
): { path: string; minY: number; maxY: number; minX: number; maxX: number } | null {
  if (points.length === 0) return null;
  const minX = Math.min(...points.map((point) => point.step));
  const maxX = Math.max(...points.map((point) => point.step));
  const rawMinY = Math.min(...points.map((point) => point.loss));
  const rawMaxY = Math.max(...points.map((point) => point.loss));
  // 全部点相等时给一个固定跨度，避免除零与一条压线
  const minY = rawMinY === rawMaxY ? rawMinY - 0.5 : rawMinY;
  const maxY = rawMinY === rawMaxY ? rawMaxY + 0.5 : rawMaxY;

  const plotWidth = width - PADDING.left - PADDING.right;
  const plotHeight = height - PADDING.top - PADDING.bottom;
  const toX = (step: number) =>
    PADDING.left + (maxX === minX ? plotWidth / 2 : ((step - minX) / (maxX - minX)) * plotWidth);
  const toY = (loss: number) =>
    PADDING.top + plotHeight - ((loss - minY) / (maxY - minY)) * plotHeight;

  const path = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${toX(point.step).toFixed(1)},${toY(point.loss).toFixed(1)}`)
    .join(' ');
  return { path, minY, maxY, minX, maxX };
}

function formatLoss(value: number): string {
  if (!Number.isFinite(value)) return '—';
  if (value === 0) return '0';
  if (Math.abs(value) < 0.01) return value.toExponential(1);
  return value.toFixed(Math.abs(value) < 1 ? 3 : 2);
}

export function LossChart({
  points,
  metric = 'loss',
  height = VIEW_HEIGHT,
}: {
  points: MetricPoint[];
  metric?: string;
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const series = useMemo(() => {
    const valid = (points ?? [])
      .map((point) => ({ step: Number(point.step), loss: Number(point[metric]) }))
      .filter((point) => Number.isFinite(point.step) && Number.isFinite(point.loss));
    return downsample(valid).sort((a, b) => a.step - b.step);
  }, [points, metric]);

  const geometry = useMemo(() => buildSeries(series), [series]);

  if (!geometry) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={`暂无训练指标（${metric}）`}
        data-testid="loss-chart-empty"
      />
    );
  }

  const { path, minY, maxY, minX, maxX } = geometry;
  const plotWidth = VIEW_WIDTH - PADDING.left - PADDING.right;
  const plotHeight = height - PADDING.top - PADDING.bottom;
  const toX = (step: number) =>
    PADDING.left + (maxX === minX ? plotWidth / 2 : ((step - minX) / (maxX - minX)) * plotWidth);
  const toY = (loss: number) =>
    PADDING.top + plotHeight - ((loss - minY) / (maxY - minY)) * plotHeight;

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    value: maxY - (maxY - minY) * ratio,
    y: PADDING.top + plotHeight * ratio,
  }));
  const xTicks = [0, 0.5, 1].map((ratio) => ({
    value: Math.round(minX + (maxX - minX) * ratio),
    x: PADDING.left + plotWidth * ratio,
  }));
  const hovered = hover !== null ? series[hover] : null;

  return (
    <div className="loss-chart" data-testid="loss-chart">
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${metric} 曲线，共 ${series.length} 个点`}
        onMouseMove={(event) => {
          // 按鼠标的水平位置找最近的 step：不用给每个点挂事件，几百个点也不会卡
          // 用 clientWidth/clientX 而不是 getBoundingClientRect：SVG 在无布局环境（单测）
          // 下 rect 恒为 0，按容器自身坐标系取点更稳
          const width = event.currentTarget.clientWidth;
          if (width === 0) return;
          const ratio = Math.min(1, Math.max(0, (event.clientX - PADDING.left) / (width - PADDING.left - PADDING.right)));
          const step = minX + ratio * (maxX - minX);
          let nearest = 0;
          for (let index = 1; index < series.length; index += 1) {
            if (Math.abs(series[index].step - step) < Math.abs(series[nearest].step - step)) {
              nearest = index;
            }
          }
          setHover(nearest);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <rect
          x={PADDING.left}
          y={PADDING.top}
          width={plotWidth}
          height={plotHeight}
          className="loss-chart-bg"
        />
        {yTicks.map((tick) => (
          <g key={`y-${tick.y}`}>
            <line
              x1={PADDING.left}
              x2={PADDING.left + plotWidth}
              y1={tick.y}
              y2={tick.y}
              className="loss-chart-grid"
            />
            <text x={PADDING.left - 8} y={tick.y + 4} textAnchor="end" className="loss-chart-axis">
              {formatLoss(tick.value)}
            </text>
          </g>
        ))}
        {xTicks.map((tick) => (
          <text
            key={`x-${tick.x}`}
            x={tick.x}
            y={PADDING.top + plotHeight + 18}
            textAnchor="middle"
            className="loss-chart-axis"
          >
            {tick.value}
          </text>
        ))}
        <path d={path} className="loss-chart-line" fill="none" />
        {hovered && (
          <line
            x1={toX(hovered.step)}
            x2={toX(hovered.step)}
            y1={PADDING.top}
            y2={PADDING.top + plotHeight}
            className="loss-chart-grid"
          />
        )}
        {hovered && (
          <circle cx={toX(hovered.step)} cy={toY(hovered.loss)} r={3.5} className="loss-chart-dot" />
        )}
      </svg>
      <div className="loss-chart-foot">
        <Typography.Text type="secondary">
          {hovered
            ? `step ${hovered.step} · ${metric} ${formatLoss(hovered.loss)}`
            : `step ${minX} → ${maxX} · ${series.length} 个点`}
        </Typography.Text>
      </div>
    </div>
  );
}
