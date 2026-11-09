import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { JobTimeline } from './JobTimeline';
import type { JobTimelineResponse } from '../types/api';

function make(data: Partial<JobTimelineResponse> = {}): JobTimelineResponse {
  return {
    name: 'job1',
    status: 'RUNNING',
    current_stage: 'running',
    items: [
      { stage: 'submitted', label: '已提交', state: 'done', at: '2026-01-01T00:00:00Z' },
      { stage: 'queued', label: '已入队', state: 'done', at: '2026-01-01T00:00:01Z' },
      { stage: 'running', label: '训练中', state: 'current', at: null },
      { stage: 'finished', label: '已结束', state: 'pending', at: null },
    ],
    ...data,
  };
}

describe('JobTimeline', () => {
  it('按顺序渲染所有阶段', () => {
    render(<JobTimeline data={make()} />);

    ['已提交', '已入队', '训练中', '已结束'].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });

  it('标注每个阶段的状态', () => {
    render(<JobTimeline data={make()} />);

    expect(screen.getAllByText(/已完成/).length).toBe(2);
    expect(screen.getByText(/进行中/)).toBeInTheDocument();
    expect(screen.getByText(/未开始/)).toBeInTheDocument();
  });

  it('没有时间戳时显示占位符而不是编造时间', () => {
    render(<JobTimeline data={make()} />);

    // running 与 finished 的 at 为 null → 两个「—」
    expect(screen.getAllByText('—').length).toBe(2);
  });

  it('失败阶段显示失败', () => {
    const data = make({
      status: 'FAILED',
      current_stage: null,
      items: [
        { stage: 'submitted', label: '已提交', state: 'done', at: null },
        { stage: 'finished', label: '已结束', state: 'failed', at: null },
      ],
    });

    render(<JobTimeline data={data} />);

    expect(screen.getByText(/失败/)).toBeInTheDocument();
  });

  it('数据为空时显示空状态而不报错', () => {
    render(<JobTimeline data={null} />);

    expect(screen.getByTestId('job-timeline')).toBeInTheDocument();
    expect(screen.getByText('暂无法获取阶段信息')).toBeInTheDocument();
  });

  it('items 为空数组时也显示空状态', () => {
    render(<JobTimeline data={make({ items: [] })} />);

    expect(screen.getByText('暂无法获取阶段信息')).toBeInTheDocument();
  });
});
