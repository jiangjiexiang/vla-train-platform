import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DatasetMeta } from '../types/api';
import { DatasetsPage } from './DatasetsPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return { ...actual, api: { listDatasets: vi.fn() } };
});

const { api } = await import('../api/client');
const listDatasetsMock = vi.mocked(api.listDatasets);

const READY: DatasetMeta = {
  id: 'pick1028',
  name: 'pick1028',
  path: '/datasets/pick1028',
  format: 'lerobot-v3.0',
  status: 'ready',
  size_bytes: 3.2 * 1024 ** 3,
  episodes: 1028,
  frames: 512000,
  fps: 30,
  tasks: ['pick the cube'],
  robot_type: 'so100',
  cameras: ['top', 'wrist'],
  image_size: [480, 640],
  state_dim: 6,
  action_dim: 6,
  action_names: [],
  state_names: [],
  duration_seconds: 600,
  issues: [],
  scanned_at: '2026-01-01T00:00:00Z',
  scan_depth: 'shallow',
  dir_mtime: 0,
};

const BROKEN: DatasetMeta = {
  ...READY,
  id: 'broken',
  name: 'broken',
  status: 'unusable',
  issues: [{ level: 'error', code: 'missing_meta', message: '缺少 meta/info.json' }],
};

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={['/datasets']}>
        <Routes>
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="/" element={<div>快速开始页面</div>} />
        </Routes>
      </MemoryRouter>
    </AntdApp>,
  );
}

beforeEach(() => {
  listDatasetsMock.mockReset();
  listDatasetsMock.mockImplementation(() => Promise.resolve({
    items: [READY, BROKEN],
    summary: { total: 2, ready: 1, warning: 0, unusable: 1, total_frames: 1024000 },
    root: '/datasets',
  }));
});

describe('DatasetsPage', () => {
  it('展示列表、统计条，点击行打开详情抽屉', async () => {
    renderPage();

    expect(await screen.findByText('pick1028')).toBeInTheDocument();
    expect(screen.getByText('数据集总数')).toBeInTheDocument();
    expect(screen.getByText('累计 1,024,000 帧')).toBeInTheDocument();

    fireEvent.click(screen.getByText('broken'));
    expect(await screen.findByText('问题清单（1）')).toBeInTheDocument();
    expect(screen.getByText(/缺少 meta\/info\.json/)).toBeInTheDocument();
    // 不可用数据集不能直接去训练
    expect(screen.getByRole('button', { name: /用此数据集开始训练/ })).toBeDisabled();
  });

  it('「重新扫描」携带 refresh=true', async () => {
    renderPage();
    await screen.findByText('pick1028');

    fireEvent.click(screen.getByTestId('rescan-button'));
    await waitFor(() => expect(listDatasetsMock).toHaveBeenLastCalledWith(true));
  });

  it('可用数据集可从抽屉跳回快速开始并预选', async () => {
    renderPage();
    await screen.findByText('pick1028');

    fireEvent.click(screen.getByText('pick1028'));
    const drawerButton = await screen.findByRole('button', { name: /用此数据集开始训练/ });
    fireEvent.click(drawerButton);

    expect(await screen.findByText('快速开始页面')).toBeInTheDocument();
  });

  it('加载失败展示错误与重试', async () => {
    listDatasetsMock.mockImplementation(() => Promise.reject(new Error('net down')));
    renderPage();

    expect(await screen.findByText('数据集加载失败')).toBeInTheDocument();
    expect(within(screen.getByRole('alert')).getByText('net down')).toBeInTheDocument();
  });
});