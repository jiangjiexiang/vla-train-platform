import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AppLayout } from '../components/AppLayout';
import { AuthProvider } from '../context/AuthContext';
import type { CurrentUser } from '../types/api';
import { UsersPage } from './UsersPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: { getHealth: vi.fn(), me: vi.fn(), users: vi.fn(), putQuota: vi.fn(), logout: vi.fn() },
  };
});

const { api, ApiError } = await import('../api/client');
const getHealthMock = vi.mocked(api.getHealth);
const meMock = vi.mocked(api.me);
const usersMock = vi.mocked(api.users);
const putQuotaMock = vi.mocked(api.putQuota);

export const ADMIN: CurrentUser = { username: 'root', role: 'admin', source: 'ldap' };
const NORMAL_USER: CurrentUser = { username: 'bob', role: 'user', source: 'ldap' };

const USERS = {
  items: [
    {
      username: 'alice',
      role: 'user' as const,
      source: 'ldap',
      last_seen_at: '2026-01-01T00:00:00Z',
      active_jobs: 2,
      gpu_in_use: 4,
      quota: { max_gpus: 4, max_active_jobs: null },
    },
    {
      username: 'root',
      role: 'admin' as const,
      source: 'ldap',
      last_seen_at: null,
      active_jobs: 0,
      gpu_in_use: 0,
      quota: { max_gpus: null, max_active_jobs: null },
    },
  ],
};

function renderWithAuth(ui: React.ReactNode, user: CurrentUser) {
  getHealthMock.mockImplementation(() => Promise.resolve({ status: 'ok', auth_required: true, auth_mode: 'ldap' }));
  meMock.mockImplementation(() => Promise.resolve(user));
  localStorage.setItem('vla-platform-token', 'session-token');
  return render(
    <AntdApp>
      <AuthProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthProvider>
    </AntdApp>,
  );
}

beforeEach(() => {
  localStorage.clear();
  getHealthMock.mockReset();
  meMock.mockReset();
  usersMock.mockReset();
  putQuotaMock.mockReset();
  // 默认实现用 mockImplementation：mockResolvedValue 设置的实现会跨用例残留
  usersMock.mockImplementation(() => Promise.resolve(USERS));
  putQuotaMock.mockImplementation(() => Promise.reject(new Error('unexpected putQuota call')));
});

describe('UsersPage', () => {
  it('管理员可以看到用户列表与配额', async () => {
    renderWithAuth(<UsersPage />, ADMIN);

    expect(await screen.findByText('alice')).toBeInTheDocument();
    expect(screen.getByText('root')).toBeInTheDocument();
    expect(screen.getByText('管理员')).toBeInTheDocument();
    expect(screen.getByText('普通用户')).toBeInTheDocument();
    // 占用与活跃任务
    expect(screen.getByText('4 卡')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    // 配额输入框回显已有值，未设置时留空表示不限
    const gpuInput = screen.getByLabelText('alice 的 GPU 配额') as HTMLInputElement;
    expect(gpuInput.value).toBe('4');
    const activeInput = screen.getByLabelText('alice 的活跃任务配额') as HTMLInputElement;
    expect(activeInput.value).toBe('');
    expect((screen.getByLabelText('root 的 GPU 配额') as HTMLInputElement).value).toBe('');
  });

  it('行内编辑配额后保存，调用 PUT /users/{name}/quota', async () => {
    putQuotaMock.mockImplementation(() => Promise.resolve({
      username: 'alice',
      quota: { max_gpus: 8, max_active_jobs: 3, gpu_in_use: 4, active_jobs: 2 },
    }));
    renderWithAuth(<UsersPage />, ADMIN);

    const gpuInput = (await screen.findByLabelText('alice 的 GPU 配额')) as HTMLInputElement;
    const row = gpuInput.closest('tr') as HTMLElement;

    // 未修改时保存按钮不可用
    expect(within(row).getByRole('button', { name: /保存/ })).toBeDisabled();

    fireEvent.change(gpuInput, { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('alice 的活跃任务配额'), { target: { value: '3' } });

    const save = within(row).getByRole('button', { name: /保存/ });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => {
      expect(putQuotaMock).toHaveBeenCalledWith('alice', { max_gpus: 8, max_active_jobs: 3 });
    });
    await waitFor(() => {
      expect((screen.getByLabelText('alice 的 GPU 配额') as HTMLInputElement).value).toBe('8');
    });
    expect((screen.getByLabelText('alice 的活跃任务配额') as HTMLInputElement).value).toBe('3');
  });

  it('清空配额输入表示不限，提交 null', async () => {
    putQuotaMock.mockImplementation(() => Promise.resolve({
      username: 'alice',
      quota: { max_gpus: null, max_active_jobs: null, gpu_in_use: 0, active_jobs: 0 },
    }));
    renderWithAuth(<UsersPage />, ADMIN);

    const gpuInput = (await screen.findByLabelText('alice 的 GPU 配额')) as HTMLInputElement;
    const row = gpuInput.closest('tr') as HTMLElement;
    fireEvent.change(gpuInput, { target: { value: '' } });
    fireEvent.blur(gpuInput);

    const save = within(row).getByRole('button', { name: /保存/ });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => {
      expect(putQuotaMock).toHaveBeenCalledWith('alice', { max_gpus: null, max_active_jobs: null });
    });
  });

  it('保存失败时保留编辑并提示错误', async () => {
    putQuotaMock.mockImplementation(() => Promise.reject(new ApiError('仅管理员可修改配额', 403)));
    renderWithAuth(<UsersPage />, ADMIN);

    const gpuInput = (await screen.findByLabelText('alice 的 GPU 配额')) as HTMLInputElement;
    const row = gpuInput.closest('tr') as HTMLElement;
    fireEvent.change(gpuInput, { target: { value: '16' } });
    fireEvent.click(within(row).getByRole('button', { name: /保存/ }));

    expect(await screen.findByText('仅管理员可修改配额')).toBeInTheDocument();
    expect((screen.getByLabelText('alice 的 GPU 配额') as HTMLInputElement).value).toBe('16');
  });

  it('非管理员访问时显示无权限页，且不请求用户列表', async () => {
    renderWithAuth(<UsersPage />, NORMAL_USER);

    expect(await screen.findByText('没有访问权限')).toBeInTheDocument();
    expect(screen.queryByText('alice')).not.toBeInTheDocument();
    // 无权限时列表本身不渲染（请求可能在角色就绪的同一批状态更新里发出，故只断言视图）
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('接口返回 403 时降级为无权限页', async () => {
    usersMock.mockImplementation(() => Promise.reject(new ApiError('Forbidden', 403)));
    renderWithAuth(<UsersPage />, ADMIN);

    expect(await screen.findByText('没有访问权限')).toBeInTheDocument();
  });

  it('列表加载失败时展示错误与重试', async () => {
    usersMock.mockImplementation(() => Promise.reject(new ApiError('集群服务暂时异常，请稍后重试或联系管理员', 503)));
    renderWithAuth(<UsersPage />, ADMIN);

    expect(await screen.findByText('用户列表加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /重试/ })).toBeInTheDocument();
  });
});

describe('AppLayout 角色菜单', () => {
  it('管理员看到「用户与配额」菜单与当前用户名', async () => {
    renderWithAuth(<AppLayout />, ADMIN);

    expect(await screen.findByText('用户与配额')).toBeInTheDocument();
    expect(await screen.findByText('root')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /退出/ })).toBeInTheDocument();
  });

  it('普通用户看不到「用户与配额」菜单', async () => {
    renderWithAuth(<AppLayout />, NORMAL_USER);

    expect(await screen.findByText('bob')).toBeInTheDocument();
    expect(screen.queryByText('用户与配额')).not.toBeInTheDocument();
    expect(screen.getByText('普通用户')).toBeInTheDocument();
  });
});

describe('匿名模式', () => {
  it('off 模式下不显示登录页，侧边栏显示匿名的管理员', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ status: 'ok', auth_required: false, auth_mode: 'off' }));

    render(
      <AntdApp>
        <AuthProvider>
          <MemoryRouter>
            <AppLayout />
          </MemoryRouter>
        </AuthProvider>
      </AntdApp>,
    );

    expect(await screen.findByText('anonymous')).toBeInTheDocument();
    expect(screen.getByText('用户与配额')).toBeInTheDocument();
    // 免登录时没有可退出的账号
    expect(screen.queryByRole('button', { name: /退出/ })).not.toBeInTheDocument();
    expect(meMock).not.toHaveBeenCalled();
  });
});