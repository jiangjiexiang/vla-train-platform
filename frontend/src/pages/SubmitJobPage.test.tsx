import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../context/AuthContext';
import type { Job, PlatformConfig } from '../types/api';
import { SubmitJobPage } from './SubmitJobPage';
import { builtinTemplate } from '../utils/buildCommand';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      getHealth: vi.fn(),
      getConfig: vi.fn(),
      getImages: vi.fn(),
      getImageParams: vi.fn(),
      me: vi.fn(),
      submitJob: vi.fn(),
    },
  };
});

const { api, ApiError } = await import('../api/client');
const getHealthMock = vi.mocked(api.getHealth);
const getConfigMock = vi.mocked(api.getConfig);
const meMock = vi.mocked(api.me);
const submitJobMock = vi.mocked(api.submitJob);
const getImagesMock = vi.mocked(api.getImages);
const getImageParamsMock = vi.mocked(api.getImageParams);

const BASE_CONFIG: PlatformConfig = {
  allowed_gpus: [1],
  default_image: 'vla-smoke:latest',
  default_command: 'python /app/train.py',
  default_queue: 'default',
  priorities: ['low', 'normal', 'high'],
  dataset_path: '/datasets',
  checkpoint_root: '/checkpoints',
};

const SUBMITTED_JOB = {
  name: 'exp001',
  status: 'QUEUED',
} as Job;

function renderPage(userRole: 'admin' | 'user' = 'user') {
  getHealthMock.mockImplementation(() => Promise.resolve({ status: 'ok', auth_required: true, auth_mode: 'ldap' }));
  // 镜像候选默认返回空列表 + 默认镜像；用例需要验证下拉选项时再覆盖
  getImagesMock.mockImplementation(() =>
    Promise.resolve({ items: [{ image: 'vla-smoke:latest', sources: ['default'] }] }),
  );
  // 模板默认返回内置通用模板（source: builtin），与后端未登记模板时的行为一致
  getImageParamsMock.mockImplementation(() =>
    Promise.resolve({ image: 'vla-smoke:latest', source: 'builtin' as const, template: builtinTemplate() }),
  );
  localStorage.setItem('vla-platform-token', 'session-token');
  // 用 mockImplementation 而不是 mockResolvedValue，避免实现泄漏到后续用例
  meMock.mockImplementation(() => Promise.resolve({
    username: 'alice',
    role: userRole,
    source: 'ldap',
    quotas: { max_gpus: 4, max_active_jobs: 2, gpu_in_use: 1, active_jobs: 1 },
  }));
  return render(
    <AntdApp>
      <AuthProvider>
        <MemoryRouter>
          <SubmitJobPage />
        </MemoryRouter>
      </AuthProvider>
    </AntdApp>,
  );
}

async function fillName(name = 'exp001') {
  const input = await screen.findByPlaceholderText('例如 exp001');
  fireEvent.change(input, { target: { value: name } });
}

beforeEach(() => {
  localStorage.clear();
  getHealthMock.mockReset();
  getConfigMock.mockReset();
  meMock.mockReset();
  submitJobMock.mockReset();
  // 统一用 mockImplementation 设置默认实现，防止实现跨用例残留
  getConfigMock.mockImplementation(() => Promise.resolve(BASE_CONFIG));
  submitJobMock.mockImplementation(() => Promise.resolve(SUBMITTED_JOB));
});

describe('SubmitJobPage v0.9 字段', () => {
  it('只读展示当前提交者（owner）与配额提示', async () => {
    renderPage();

    expect(await screen.findByText('提交者（owner）')).toBeInTheDocument();
    await waitFor(() => expect(meMock).toHaveBeenCalled());
    expect(await screen.findByText('alice')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('alice')).not.toBeInTheDocument();
    // GPU 数量字段的辅助说明里给出配额与剩余量
    const hint = await screen.findByTestId('quota-hint');
    await waitFor(() => expect(hint).toHaveTextContent('GPU 配额：已用 1 / 4 卡，剩余 3 卡'));
    expect(hint).toHaveTextContent('活跃任务：1 / 2，剩余 1 个');

  });

  it('max_nodes 为 1 时不显示多机选项', async () => {
    renderPage();

    await screen.findByText('提交者（owner）');
    expect(screen.queryByTestId('multi-node-options')).not.toBeInTheDocument();
    expect(screen.queryByText('多机训练')).not.toBeInTheDocument();
  });

  it('max_nodes > 1 时显示多机选项，开启后按节点数 × 每节点 GPU 提交', async () => {
    getConfigMock.mockImplementation(() => Promise.resolve({ ...BASE_CONFIG, allowed_gpus: [1, 2], max_nodes: 4 }));
    renderPage();

    const block = await screen.findByTestId('multi-node-options');
    fireEvent.click(within(block).getByRole('switch'));

    const nodesInput = within(block).getByRole('spinbutton');
    fireEvent.change(nodesInput, { target: { value: '2' } });
    fireEvent.blur(nodesInput);

    // 每节点 GPU 选择 2 卡
    const perNodeSelect = screen.getByText('每节点 GPU').closest('.ant-form-item') as HTMLElement;
    fireEvent.mouseDown(within(perNodeSelect).getByRole('combobox'));
    fireEvent.click(await screen.findByTitle('2 卡'));

    expect(await screen.findByTestId('total-gpus')).toHaveTextContent('4 卡');

    await fillName();
    fireEvent.click(screen.getByRole('button', { name: /提交训练任务/ }));

    await waitFor(() => expect(submitJobMock).toHaveBeenCalledTimes(1));
    const payload = submitJobMock.mock.calls[0][0];
    expect(payload.gpus).toBe(4);
    expect(payload.nodes).toBe(2);
    expect(payload.gpus_per_node).toBe(2);
  });

  it('多机关闭时提交 nodes=1，且不带 gpus_per_node', async () => {
    getConfigMock.mockImplementation(() => Promise.resolve({ ...BASE_CONFIG, max_nodes: 4 }));
    renderPage();

    await screen.findByTestId('multi-node-options');
    await fillName();
    fireEvent.click(screen.getByRole('button', { name: /提交训练任务/ }));

    await waitFor(() => expect(submitJobMock).toHaveBeenCalledTimes(1));
    const payload = submitJobMock.mock.calls[0][0];
    expect(payload.nodes).toBe(1);
    expect(payload.gpus_per_node).toBeUndefined();
    expect(payload.gpus).toBe(1);
  });

  it('标签以键值对形式提交', async () => {
    renderPage();

    await screen.findByText('任务标签');
    fireEvent.click(screen.getByRole('button', { name: /添加标签/ }));
    fireEvent.change(await screen.findByPlaceholderText('标签键，例如 team'), { target: { value: 'team' } });
    fireEvent.change(screen.getByPlaceholderText('标签值，例如 perception'), { target: { value: 'perception' } });

    await fillName();
    fireEvent.click(screen.getByRole('button', { name: /提交训练任务/ }));

    await waitFor(() => expect(submitJobMock).toHaveBeenCalledTimes(1));
    expect(submitJobMock.mock.calls[0][0].labels).toEqual({ team: 'perception' });
  });

  it('标签键格式非法时阻止提交并提示', async () => {
    renderPage();

    await screen.findByText('任务标签');
    fireEvent.click(screen.getByRole('button', { name: /添加标签/ }));
    fireEvent.change(await screen.findByPlaceholderText('标签键，例如 team'), { target: { value: 'Team_Name' } });
    fireEvent.change(screen.getByPlaceholderText('标签值，例如 perception'), { target: { value: 'perception' } });

    await fillName();
    fireEvent.click(screen.getByRole('button', { name: /提交训练任务/ }));

    expect(await screen.findByText('标签键格式无效')).toBeInTheDocument();
    expect(submitJobMock).not.toHaveBeenCalled();
  });

  it('配额超限（422）时提示原因并跳转任务列表', async () => {
    submitJobMock.mockImplementation(() => Promise.reject(new ApiError('GPU quota exceeded: 4 used of 4 allowed', 422)));
    renderPage();

    await fillName('exp-quota');
    fireEvent.click(screen.getByRole('button', { name: /提交训练任务/ }));

    expect(await screen.findByText('GPU quota exceeded: 4 used of 4 allowed')).toBeInTheDocument();
  });
});
