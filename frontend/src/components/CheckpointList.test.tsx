import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CheckpointList, formatBytes } from './CheckpointList';
import type { JobCheckpointsResponse } from '../types/api';

function make(overrides: Partial<JobCheckpointsResponse> = {}): JobCheckpointsResponse {
  return {
    name: 'job1',
    exists: true,
    path: '/checkpoints/job1',
    total_bytes: 4096,
    truncated: false,
    items: [
      {
        name: 'smoke.pt',
        size_bytes: 3352,
        modified_at: '2026-01-01T00:00:00Z',
        is_weights: true,
      },
      {
        name: 'metrics.jsonl',
        size_bytes: 744,
        modified_at: '2026-01-01T00:00:01Z',
        is_weights: false,
      },
    ],
    ...overrides,
  };
}

describe('formatBytes', () => {
  it('把字节数压成可读单位', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(200 * 1024 * 1024)).toBe('200 MB');
  });

  it('对负数与非法值兜底', () => {
    expect(formatBytes(-1)).toBe('0 B');
  });
});

describe('CheckpointList', () => {
  it('列出文件并区分权重与其它产物', () => {
    render(<CheckpointList data={make()} />);

    expect(screen.getByText('smoke.pt')).toBeInTheDocument();
    expect(screen.getByText('metrics.jsonl')).toBeInTheDocument();
    expect(screen.getByText('模型权重')).toBeInTheDocument();
    expect(screen.getByText('其它')).toBeInTheDocument();
  });

  it('标题里给出权重数量与总大小', () => {
    render(<CheckpointList data={make()} />);

    expect(screen.getByText(/1 个权重文件/)).toBeInTheDocument();
    expect(screen.getByText(/4\.0 KB/)).toBeInTheDocument();
  });

  it('没有产物时提示而不是空白', () => {
    render(<CheckpointList data={make({ exists: false, items: [] })} />);

    expect(screen.getByText(/还没有产物/)).toBeInTheDocument();
  });

  it('数据为 null 时也不报错', () => {
    render(<CheckpointList data={null} />);

    expect(screen.getByTestId('job-checkpoints')).toBeInTheDocument();
  });

  it('产物过多时提示已截断', () => {
    render(<CheckpointList data={make({ truncated: true })} />);

    expect(screen.getByText(/仅显示前 200 项/)).toBeInTheDocument();
  });
});
