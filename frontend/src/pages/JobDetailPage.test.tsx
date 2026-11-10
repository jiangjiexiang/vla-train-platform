import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Job, JobMetricsResponse, TensorboardResponse } from '../types/api';
import { JobDetailPage } from './JobDetailPage';

// 只 mock 详情页真正用到的两个接口。TensorBoard 走真接口 client，
// 这样 "页面加载时不请求 tensorboard" 这条断言才是在验证真实行为，
// 而不是验证一个 mock 自己有没有被调用。
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      getJob: vi.fn(),
      getJobMetrics: vi.fn(),
      getJobTensorboard: vi.fn(),
      // 渲染时不会主动调用，留空实现只为 JobActions 等组件的引用不为 undefined
      cancelJob: vi.fn(),
      cloneJob: vi.fn(),
    },
  };
});

const { api } = await import('../api/client');
const getJobMock = vi.mocked(api.getJob);
const getJobMetricsMock = vi.mocked(api.getJobMetrics);
const getJobTensorboardMock = vi.mocked(api.getJobTensorboard);

const JOB: Job = {
  name: 'test2',
  image: 'vla-smolvla:latest',
  gpus: 1,
  queue: 'default',
  priority: 'normal',
  command: 'python /app/train.py',
  env: {},
  max_retries: 0,
  termination_grace_period_seconds: 30,
  status: 'SUCCEEDED',
  created_at: '2026-11-10T09:00:00Z',
  started_at: '2026-11-10T09:01:00Z',
  finished_at: '2026-11-10T09:30:00Z',
  duration_seconds: 1740,
  workload_status: 'finished',
  pod_status: 'Succeeded',
  message: null,
  checkpoint_dir: '/checkpoints/test2',
  owner: 'jjx',
  nodes: 1,
};

const METRICS: JobMetricsResponse = {
  name: 'test2',
  items: [
    { step: 1, loss: 1.3 },
    { step: 2, loss: 1.1 },
  ],
  total_points: 2,
  truncated: false,
  path_exists: true,
};

// 与真实返回一致：tag 是裸键，step 从 1 开始
const TENSORBOARD: TensorboardResponse = {
  name: 'test2',
  tags: ['loss', 'grad_norm', 'lr', 'update_s'],
  series: {
    loss: [
      { step: 1, value: 1.3198, wall_time: 1794278413.9 },
      { step: 2, value: 1.2011, wall_time: 1794278414.9 },
    ],
    grad_norm: [
      { step: 1, value: 8.44, wall_time: 1794278413.9 },
      { step: 2, value: 12.07, wall_time: 1794278414.9 },
    ],
    lr: [
      { step: 1, value: 0.0001, wall_time: 1794278413.9 },
      { step: 2, value: 0.0001, wall_time: 1794278414.9 },
    ],
    update_s: [
      { step: 1, value: 0.42, wall_time: 1794278413.9 },
      { step: 2, value: 0.4, wall_time: 1794278414.9 },
    ],
  },
  path_exists: true,
  files: ['events.out.tfevents.1794278384.test2-master-0.83.0'],
  total_points: 8,
  truncated: false,
  dir: '/data/vla-platform/checkpoints/test2/tensorboard',
};

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={['/jobs/test2']}>
        <Routes>
          <Route path="/jobs/:name" element={<JobDetailPage />} />
        </Routes>
      </MemoryRouter>
    </AntdApp>,
  );
}

function chartLabel(): string | null {
  return screen.getByTestId('loss-chart').querySelector('svg')?.getAttribute('aria-label') ?? null;
}

/** 切到 TensorBoard 标签。Segmented 的字号随机不保证，按文本点。 */
async function switchToTensorboard() {
  const sourceSwitch = await screen.findByTestId('metric-source-switch');
  fireEvent.click(within(sourceSwitch).getByText('TensorBoard'));
}

beforeEach(() => {
  getJobMock.mockReset();
  getJobMetricsMock.mockReset();
  getJobTensorboardMock.mockReset();
  getJobMock.mockResolvedValue(JOB);
  getJobMetricsMock.mockResolvedValue(METRICS);
  getJobTensorboardMock.mockResolvedValue(TENSORBOARD);
});

describe('JobDetailPage TensorBoard', () => {
  it('页面加载时不请求 TensorBoard，切到该标签后才按需加载并画图', async () => {
    renderPage();

    // 等平台指标先渲染出来，确认详情页已加载完
    expect(await screen.findByTestId('metric-switch')).toBeInTheDocument();
    expect(getJobTensorboardMock).not.toHaveBeenCalled();

    await switchToTensorboard();

    // 按需加载：切过去才发请求，且请求的是当前任务
    await waitFor(() => expect(getJobTensorboardMock).toHaveBeenCalledTimes(1));
    expect(getJobTensorboardMock).toHaveBeenCalledWith('test2', 2000, expect.anything());

    // 有数据时直接复用 LossChart 画图
    expect(await screen.findByTestId('loss-chart')).toBeInTheDocument();
    expect(screen.getByTestId('loss-chart')).not.toBeEmptyDOMElement();
    expect(screen.getByTestId('tensorboard-point-count')).toHaveTextContent('2 个点');
    // 没被抽稀就不要吓唬用户
    expect(screen.queryByTestId('tensorboard-truncated')).not.toBeInTheDocument();
  });

  it('path_exists=false 时显示 Empty，并说明是新镜像才有的能力', async () => {
    getJobTensorboardMock.mockResolvedValue({
      ...TENSORBOARD,
      tags: [],
      series: {},
      files: [],
      total_points: 0,
      path_exists: false,
    });
    renderPage();
    await screen.findByTestId('metric-source-switch');

    await switchToTensorboard();

    const empty = await screen.findByTestId('tensorboard-empty');
    expect(empty).toHaveTextContent(
      '该任务还没生成 TensorBoard 数据。用新镜像（2026-11-10 之后构建的 vla-smolvla）提交的任务才会有。',
    );
    // 不画图，也不出现报错（文件不存在是正常状态，不是错误）
    expect(screen.queryByTestId('loss-chart')).not.toBeInTheDocument();
    expect(screen.queryByText('TensorBoard 数据加载失败')).not.toBeInTheDocument();
  });

  it('切换指标能正常渲染对应 tag 的曲线', async () => {
    getJobTensorboardMock.mockResolvedValue({
      ...TENSORBOARD,
      truncated: true,
    });
    renderPage();
    await screen.findByTestId('metric-source-switch');
    await switchToTensorboard();

    const tagSwitch = await screen.findByTestId('tensorboard-tag-switch');
    // 默认选中第一个 tag（loss）
    expect(within(tagSwitch).getByText('loss')).toBeInTheDocument();
    // aria-label 挂在 LossChart 内部的 svg 上，data-testid 在外层容器上
    expect(chartLabel()).toBe('loss 曲线，共 2 个点');
    expect(within(screen.getByTestId('loss-chart')).getByText('step 1 → 2 · 2 个点')).toBeInTheDocument();

    fireEvent.click(within(tagSwitch).getByText('梯度范数'));

    await screen.findByTestId('loss-chart');
    expect(chartLabel()).toBe('grad_norm 曲线，共 2 个点');
    // 2 个点、step 1→2 都来自 grad_norm 自己的序列
    expect(within(screen.getByTestId('loss-chart')).getByText('step 1 → 2 · 2 个点')).toBeInTheDocument();

    // truncated=true 时给出抽稀提示
    expect(screen.getByTestId('tensorboard-truncated')).toHaveTextContent('数据点较多，已抽稀显示');
  });

  it('接口报错时只在区块内提示，不影响其他区块', async () => {
    getJobTensorboardMock.mockRejectedValue(new Error('job test2 not found'));
    renderPage();
    await screen.findByTestId('metric-source-switch');

    await switchToTensorboard();

    expect(await screen.findByText('TensorBoard 数据加载失败')).toBeInTheDocument();
    expect(screen.getByText('job test2 not found')).toBeInTheDocument();

    // 其他区块照常：任务标题、平台指标标签页里的曲线都还在
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('test2');
    fireEvent.click(screen.getByText('平台指标'));
    expect(await screen.findByTestId('metric-switch')).toBeInTheDocument();
    expect(screen.getByTestId('loss-chart')).toBeInTheDocument();
  });
});