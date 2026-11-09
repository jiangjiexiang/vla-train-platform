import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../context/AuthContext';
import { LoginPage } from './LoginPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return { ...actual, api: { getHealth: vi.fn(), getConfig: vi.fn(), me: vi.fn(), login: vi.fn(), logout: vi.fn() } };
});

const { api, ApiError } = await import('../api/client');
const getHealthMock = vi.mocked(api.getHealth);
const getConfigMock = vi.mocked(api.getConfig);
const meMock = vi.mocked(api.me);
const loginMock = vi.mocked(api.login);

const HEALTH_OK = { status: 'ok' };

// AuthProvider 的 /health 探测在渲染后异步落地；在 act 内让出一帧，
// 让这条探测链复位（不会在提交后触发「已登录 → 跳转仪表盘」）
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

function renderLogin(initialEntries: Array<string | { pathname: string; state?: unknown }> = ['/login']) {
  return render(
    <AntdApp>
      <AuthProvider>
        <MemoryRouter initialEntries={initialEntries}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/dashboard" element={<div>集群总览页面</div>} />
            <Route path="/jobs" element={<div>训练任务页面</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </AntdApp>,
  );
}

beforeEach(() => {
  localStorage.clear();
  getHealthMock.mockReset();
  getConfigMock.mockReset();
  meMock.mockReset();
  loginMock.mockReset();
  // vi.fn() 的默认实现会返回上一轮设置的实现；显式兜底，避免用例之间相互污染
  getConfigMock.mockImplementation(() => Promise.reject(new Error('unexpected getConfig in test')));
  meMock.mockImplementation(() => Promise.reject(new Error('unexpected me in test')));
  loginMock.mockImplementation(() => Promise.reject(new Error('unexpected login in test')));
  // /health 探测延到下一个宏任务才落地：探测触发的状态更新因此落在 render 的 act 作用域内，
  // 不会与随后登录提交产生的状态更新交错（真实网络请求本来就不是同步完成的）
  getHealthMock.mockImplementation(() => Promise.reject(new Error('health unavailable')));
});

describe('LoginPage', () => {
  it('token 模式默认展示访问令牌输入框', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'token' }));

    renderLogin();

    expect(await screen.findByLabelText('访问令牌')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('请输入平台 Bearer Token')).toBeInTheDocument();
    expect(screen.queryByLabelText('用户名')).not.toBeInTheDocument();
  });

  it('token 模式提交后校验令牌并跳转仪表盘', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'token' }));
    getConfigMock.mockImplementation(() => Promise.resolve({} as never));
    meMock.mockImplementation(() => Promise.resolve({ username: 'token', role: 'admin', source: 'token' }));

    renderLogin();

    fireEvent.change(await screen.findByPlaceholderText('请输入平台 Bearer Token'), {
      target: { value: ' shared-token ' },
    });
    fireEvent.click(screen.getByRole('button', { name: '验证并登录' }));

    expect(await screen.findByText('集群总览页面')).toBeInTheDocument();
    expect(getConfigMock).toHaveBeenCalledWith(undefined, 'shared-token');
    expect(localStorage.getItem('vla-platform-static-token')).toBe('shared-token');
  });

  it('token 无效时展示后端返回的错误且停留在登录页', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'token' }));
    getConfigMock.mockImplementation(() => Promise.reject(new Error('访问令牌无效或已过期，请重新登录')));

    renderLogin();

    fireEvent.change(await screen.findByPlaceholderText('请输入平台 Bearer Token'), {
      target: { value: 'bad-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: '验证并登录' }));

    await settle();
    await waitFor(() => expect(getConfigMock).toHaveBeenCalledWith(undefined, 'bad-token'));
    // 令牌未写入：失败的登录不会留下任何凭据
    expect(localStorage.getItem('vla-platform-static-token')).toBeNull();
    await waitFor(() => {
      expect(document.body.textContent).toContain('访问令牌无效或已过期，请重新登录');
    });
  });

  it('ldap 模式展示用户名/密码表单，提交后保存会话令牌', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'ldap' }));
    loginMock.mockImplementation(() => Promise.resolve({
      token: 'session-xyz',
      expires_at: '2026-01-01T00:00:00Z',
      user: { username: 'alice', role: 'user', source: 'ldap' },
    }));

    renderLogin();

    fireEvent.change(await screen.findByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } });
    fireEvent.click(screen.getByRole('button', { name: /^登\s*录$/ }));

    expect(await screen.findByText('集群总览页面')).toBeInTheDocument();
    expect(loginMock).toHaveBeenCalledWith('alice', 'secret');
    expect(localStorage.getItem('vla-platform-token')).toBe('session-xyz');
    // ldap 登录不走静态令牌校验
    expect(getConfigMock).not.toHaveBeenCalled();
  });

  it('ldap 登录失败时展示统一的错误信息', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'ldap' }));
    loginMock.mockImplementation(() => Promise.reject(new Error('Invalid username or password')));

    renderLogin();

    fireEvent.change(await screen.findByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: /^登\s*录$/ }));

    expect(await screen.findByText('Invalid username or password')).toBeInTheDocument();
    expect(localStorage.getItem('vla-platform-token')).toBeNull();
  });

  it('LDAP 不可达（503）时提示认证服务不可用而不是密码错误', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'ldap' }));
    loginMock.mockImplementation(() => Promise.reject(new ApiError('LDAP server unavailable', 503)));

    renderLogin();

    fireEvent.change(await screen.findByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } });
    fireEvent.click(screen.getByRole('button', { name: /^登\s*录$/ }));

    expect(await screen.findByText('认证服务不可用')).toBeInTheDocument();
    expect(screen.getByText(/LDAP 暂时无法连接/)).toBeInTheDocument();
  });

  it('ldap 模式下仍可手动切换到访问令牌登录', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'ldap' }));

    renderLogin();

    expect(await screen.findByLabelText('用户名')).toBeInTheDocument();
    fireEvent.click(screen.getByText('令牌登录'));
    expect(await screen.findByLabelText('访问令牌')).toBeInTheDocument();
  });

  it('匿名（off）模式不显示登录页，直接进入仪表盘', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ status: 'ok', auth_required: false, auth_mode: 'off' }));

    renderLogin();

    expect(await screen.findByText('集群总览页面')).toBeInTheDocument();
    expect(screen.queryByLabelText('访问令牌')).not.toBeInTheDocument();
  });

  it('登录后回到来源页面', async () => {
    getHealthMock.mockImplementation(() => Promise.resolve({ ...HEALTH_OK, auth_required: true, auth_mode: 'token' }));
    getConfigMock.mockImplementation(() => Promise.resolve({} as never));
    meMock.mockImplementation(() => Promise.resolve({ username: 'token', role: 'admin', source: 'token' }));

    renderLogin(['/', { pathname: '/login', state: { from: '/jobs' } }]);

    fireEvent.change(await screen.findByPlaceholderText('请输入平台 Bearer Token'), {
      target: { value: 'shared-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: '验证并登录' }));

    expect(await screen.findByText('训练任务页面')).toBeInTheDocument();
  });

  it('健康检查失败时提示后端不可用而不是白屏', async () => {
    getHealthMock.mockImplementation(() => Promise.reject(new Error('无法连接 API 服务，请检查网络连接和后端服务状态')));

    renderLogin();

    await waitFor(() => {
      expect(screen.getByText('无法连接 API 服务，请检查网络连接和后端服务状态')).toBeInTheDocument();
    });
  });
});