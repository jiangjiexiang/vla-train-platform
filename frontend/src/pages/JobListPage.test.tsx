import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { JobListPage } from './JobListPage';

vi.mock('../api/client', () => ({
  api: { getJobs: vi.fn() },
  getErrorMessage: (error: unknown) => (error instanceof Error ? error.message : '发生未知错误'),
  isAbortError: (error: unknown) =>
    error instanceof DOMException && error.name === 'AbortError',
}));

const getJobsMock = vi.mocked(api.getJobs);

function emptyResponse(total = 0) {
  return { items: [], total, limit: 20, offset: 0 };
}

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter>
        <JobListPage />
      </MemoryRouter>
    </AntdApp>,
  );
}

beforeEach(() => {
  getJobsMock.mockReset();
  getJobsMock.mockResolvedValue(emptyResponse());
});

describe('JobListPage', () => {
  it('requests the first page with the default page size', async () => {
    renderPage();

    await waitFor(() => expect(getJobsMock).toHaveBeenCalledTimes(1));
    expect(getJobsMock).toHaveBeenCalledWith({ limit: 20, offset: 0 }, expect.anything());
    expect(await screen.findByText('还没有任务，去「快速开始」一键发起训练吧')).toBeInTheDocument();
  });

  it('sends the selected status, resets to page 1 and uses the filtered empty text', async () => {
    getJobsMock.mockResolvedValue(emptyResponse(45));
    const { container } = renderPage();

    await waitFor(() => expect(getJobsMock).toHaveBeenCalledTimes(1));

    // 先翻到第 2 页，确认筛选变化后会重置 offset
    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => {
      expect(getJobsMock).toHaveBeenLastCalledWith({ limit: 20, offset: 20 }, expect.anything());
    });

    const statusFilter = container.querySelector('.status-filter');
    expect(statusFilter).not.toBeNull();
    fireEvent.mouseDown(within(statusFilter as HTMLElement).getByRole('combobox'));
    fireEvent.click(await screen.findByText('运行中'));

    await waitFor(() => {
      expect(getJobsMock).toHaveBeenLastCalledWith(
        { limit: 20, offset: 0, status: 'RUNNING' },
        expect.anything(),
      );
    });
    expect(await screen.findByText('没有匹配的任务，请调整筛选条件')).toBeInTheDocument();
  });

  it('debounces the task-name search and resets to page 1', async () => {
    getJobsMock.mockResolvedValue(emptyResponse(45));
    renderPage();

    await waitFor(() => expect(getJobsMock).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => {
      expect(getJobsMock).toHaveBeenLastCalledWith({ limit: 20, offset: 20 }, expect.anything());
    });

    fireEvent.change(screen.getByPlaceholderText('搜索任务名'), { target: { value: 'llama' } });

    // 防抖窗口内不应立刻发起搜索请求
    expect(getJobsMock).toHaveBeenLastCalledWith({ limit: 20, offset: 20 }, expect.anything());

    await waitFor(
      () => {
        expect(getJobsMock).toHaveBeenLastCalledWith(
          { limit: 20, offset: 0, q: 'llama' },
          expect.anything(),
        );
      },
      { timeout: 1500 },
    );
  });
});