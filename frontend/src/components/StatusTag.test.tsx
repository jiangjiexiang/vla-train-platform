import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PriorityTag, StatusTag } from './StatusTag';

const statusCases: Array<[status: string, label: string, color: string]> = [
  ['QUEUED', '排队中', 'gold'],
  ['RUNNING', '运行中', 'processing'],
  ['SUCCEEDED', '已成功', 'success'],
  ['FAILED', '已失败', 'error'],
  ['CANCELLED', '已取消', 'default'],
  ['UNKNOWN', '状态未知', 'purple'],
];

describe('StatusTag', () => {
  it.each(statusCases)('maps %s to "%s" with the %s color', (status, label, color) => {
    const { container } = render(<StatusTag status={status} />);

    expect(screen.getByText(label)).toBeInTheDocument();
    expect(container.querySelector('.ant-tag')).toHaveClass(`ant-tag-${color}`);
  });

  it('falls back to the unknown mapping for an unrecognized status', () => {
    const { container } = render(<StatusTag status="SOMETHING_NEW" />);

    expect(screen.getByText('状态未知')).toBeInTheDocument();
    expect(container.querySelector('.ant-tag')).toHaveClass('ant-tag-purple');
  });
});

describe('PriorityTag', () => {
  it.each([
    ['low', '低', 'default'],
    ['normal', '普通', 'blue'],
    ['high', '高', 'orange'],
  ] as const)('maps %s to "%s" with the %s color', (priority, label, color) => {
    const { container } = render(<PriorityTag priority={priority} />);

    expect(screen.getByText(label)).toBeInTheDocument();
    expect(container.querySelector('.ant-tag')).toHaveClass(`ant-tag-${color}`);
  });

  it('renders an unknown priority as-is with the default color', () => {
    const { container } = render(<PriorityTag priority="urgent" />);

    expect(screen.getByText('urgent')).toBeInTheDocument();
    expect(container.querySelector('.ant-tag')).toHaveClass('ant-tag-default');
  });
});
