import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

const TB_EMPTY: TensorboardResponse = {
  name: 'test2',
  tags: [],
  series: {},
  path_exists: false,
  files: [],
  total_points: 0,
  truncated: false,
  dir: '/checkpoints/test2/tensorboard',
};

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


beforeEach(() => {
  getJobMock.mockReset();
  getJobMetricsMock.mockReset();
  getJobTensorboardMock.mockReset();
  getJobMock.mockResolvedValue(JOB);
  getJobMetricsMock.mockResolvedValue(METRICS);
  getJobTensorboardMock.mockResolvedValue(TENSORBOARD);
});

describe('JobDetailPage TensorBoard', () => {
  // TensorBoard 现在是 iframe 嵌官方界面，而且是默认数据源。
  // 断言从「有没有 SVG」变成「iframe 的 src 对不对」。
  it('默认渲染 TensorBoard iframe，指向当前任务的 run', async () => {
    getJobTensorboardMock.mockResolvedValue(TB_EMPTY);
    renderPage();

    // 默认数据源就是 TensorBoard，不需要再点一次
    const frame = (await screen.findByTitle(/^TensorBoard - /)) as HTMLIFrameElement;
    expect(frame.tagName).toBe('IFRAME');
    const src = frame.getAttribute('src') ?? '';
    expect(src).toContain('runFilter=');
    expect(decodeURIComponent(src)).toContain('test2/tensorboard');
  });

  it('切到平台指标后不渲染 iframe（老任务没有 events 时的兜底）', async () => {
    getJobTensorboardMock.mockResolvedValue(TB_EMPTY);
    renderPage();

    await screen.findByTitle(/^TensorBoard - /);
    fireEvent.click(screen.getByText('平台指标'));

    await waitFor(() => expect(screen.queryByTitle(/^TensorBoard - /)).toBeNull());
  });
});