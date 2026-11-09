import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  API_BASE,
  ApiError,
  TOKEN_STORAGE_KEY,
  UNAUTHORIZED_EVENT,
  api,
  clearStoredTokens,
  getErrorMessage,
  getSessionToken,
  getStaticToken,
  getStoredToken,
  isAbortError,
  isForbidden,
  isUnauthorized,
  storeSessionToken,
  storeStaticToken,
} from './client';

const fetchMock = vi.fn();

function mockResponse(status: number, payload?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response;
}

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  // 默认实现显式兜底（mockResolvedValue 设置的实现会跨用例残留）
  fetchMock.mockImplementation(() => Promise.reject(new Error('unexpected fetch call')));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api request handling', () => {
  it('returns parsed JSON and sends the stored token for successful calls', async () => {
    const payload = { items: [], total: 0 };
    localStorage.setItem(TOKEN_STORAGE_KEY, 'token-123');
    fetchMock.mockResolvedValue(mockResponse(200, payload));

    await expect(api.getJobs()).resolves.toBe(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/jobs`);
    expect(init.method).toBeUndefined();
    const headers = init.headers as Headers;
    expect(headers.get('Accept')).toBe('application/json');
    expect(headers.get('Authorization')).toBe('Bearer token-123');
  });

  it('does not send a token when the call explicitly opts out', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'token-123');
    fetchMock.mockResolvedValue(mockResponse(200, { status: 'ok' }));

    await api.getHealth();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).has('Authorization')).toBe(false);
  });

  it('returns undefined for a 204 response without parsing a body', async () => {
    const json = vi.fn();
    fetchMock.mockResolvedValue({ ok: true, status: 204, json } as unknown as Response);

    await expect(api.cancelJob('job-1')).resolves.toBeUndefined();

    expect(json).not.toHaveBeenCalled();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/jobs/job-1`);
    expect(init.method).toBe('DELETE');
  });

  it('dispatches UNAUTHORIZED_EVENT on 401 when a token was sent', async () => {
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-token');
    fetchMock.mockResolvedValue(mockResponse(401, { detail: '令牌已过期' }));

    await expect(api.getJobs()).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
      message: '令牌已过期',
    });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it('does not dispatch UNAUTHORIZED_EVENT on 401 without a token', async () => {
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    fetchMock.mockResolvedValue(mockResponse(401, { detail: '未认证' }));

    await expect(api.getHealth()).rejects.toMatchObject({
      status: 401,
      message: '未认证',
    });
    expect(onUnauthorized).not.toHaveBeenCalled();

    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it('uses the detail message for a 404 and falls back when detail is absent', async () => {
    fetchMock.mockResolvedValue(mockResponse(404, { detail: '任务不存在' }));
    await expect(api.getJob('missing')).rejects.toMatchObject({
      status: 404,
      message: '任务不存在',
    });

    fetchMock.mockResolvedValue(mockResponse(404, null));
    await expect(api.getJob('missing')).rejects.toMatchObject({
      status: 404,
      message: '请求的资源不存在',
    });
  });

  it('joins array details from validation errors for a 500', async () => {
    fetchMock.mockResolvedValue(
      mockResponse(500, { detail: [{ msg: '字段必填' }, { msg: '类型错误' }, '第三条'] }),
    );

    await expect(api.getJobs()).rejects.toMatchObject({
      status: 500,
      message: '字段必填；类型错误；第三条',
    });
  });

  it('falls back to the status message for unknown error payload shapes', async () => {
    fetchMock.mockResolvedValue(mockResponse(500, { detail: { nested: true } }));
    await expect(api.getJobs()).rejects.toMatchObject({
      status: 500,
      message: '集群服务暂时异常，请稍后重试或联系管理员',
    });

    fetchMock.mockResolvedValue(mockResponse(400, ['no-detail-field']));
    await expect(api.getJobs()).rejects.toMatchObject({
      status: 400,
      message: '请求失败（HTTP 400）',
    });

    fetchMock.mockResolvedValue(mockResponse(400, null));
    await expect(api.getJobs()).rejects.toMatchObject({
      status: 400,
      message: '请求失败（HTTP 400）',
    });
  });

  it('uses the status fallback when an error body is not valid JSON', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 503,
      json: vi.fn().mockRejectedValue(new SyntaxError('Unexpected token')),
    } as unknown as Response);

    await expect(api.getJobs()).rejects.toMatchObject({
      status: 503,
      message: '集群服务暂时异常，请稍后重试或联系管理员',
    });
  });

  it('wraps network failures in an ApiError with status 0', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(api.getJobs()).rejects.toMatchObject({
      name: 'ApiError',
      status: 0,
      message: '无法连接 API 服务，请检查网络连接和后端服务状态',
    });
  });

  it('rethrows abort errors unchanged', async () => {
    const abortError = new DOMException('Aborted', 'AbortError');
    fetchMock.mockRejectedValue(abortError);

    await expect(api.getJobs()).rejects.toBe(abortError);
  });

  it('propagates server-provided details through ApiError', async () => {
    fetchMock.mockResolvedValue(mockResponse(403, { detail: '没有权限' }));

    const error = await api.getJobs().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(getErrorMessage(error)).toBe('没有权限');
  });
});

describe('getJobs query parameters', () => {
  beforeEach(() => {
    fetchMock.mockImplementation(() => Promise.resolve(mockResponse(200, { items: [], total: 0 })));
  });

  it('builds limit/offset/status/q into the query string', async () => {
    await api.getJobs({ limit: 20, offset: 40, status: 'RUNNING', q: 'llama 3' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${API_BASE}/jobs?limit=20&offset=40&status=RUNNING&q=llama+3`);
  });

  it('omits null, undefined and blank parameters', async () => {
    await api.getJobs({ limit: null, offset: undefined, status: null, q: '   ' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${API_BASE}/jobs`);
  });

  it('keeps offset=0 and trims the search keyword', async () => {
    await api.getJobs({ limit: 20, offset: 0, q: '  job-1  ' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${API_BASE}/jobs?limit=20&offset=0&q=job-1`);
  });

  it('passes the abort signal through the request options', async () => {
    const controller = new AbortController();

    await api.getJobs({ limit: 20 }, controller.signal);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });
});

describe('getErrorMessage', () => {
  it('prefers ApiError and Error messages', () => {
    expect(getErrorMessage(new ApiError('接口错误', 500))).toBe('接口错误');
    expect(getErrorMessage(new Error('普通错误'))).toBe('普通错误');
  });

  it('returns a generic message for non-Error values', () => {
    expect(getErrorMessage('boom')).toBe('发生未知错误，请稍后重试');
    expect(getErrorMessage(null)).toBe('发生未知错误，请稍后重试');
    expect(getErrorMessage({ message: 'nope' })).toBe('发生未知错误，请稍后重试');
  });
});

describe('isAbortError', () => {
  it('detects only DOMException abort errors', () => {
    expect(isAbortError(new DOMException('Aborted', 'AbortError'))).toBe(true);
    expect(isAbortError(new DOMException('Timed out', 'TimeoutError'))).toBe(false);
    expect(isAbortError(new Error('AbortError'))).toBe(false);
    expect(isAbortError(null)).toBe(false);
  });
});

describe('v0.9 鉴权接口', () => {
  it('login 以匿名身份 POST 账号密码，且 401 不触发全局失效事件', async () => {
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    localStorage.setItem(TOKEN_STORAGE_KEY, 'stale-token');
    fetchMock.mockResolvedValue(mockResponse(401, { detail: 'Invalid username or password' }));

    await expect(api.login('alice', 'wrong')).rejects.toMatchObject({
      status: 401,
      message: 'Invalid username or password',
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/auth/login`);
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ username: 'alice', password: 'wrong' }));
    expect((init.headers as Headers).has('Authorization')).toBe(false);
    expect(onUnauthorized).not.toHaveBeenCalled();

    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it('login 成功时原样返回令牌与过期时间', async () => {
    const payload = {
      token: 'session-abc',
      expires_at: '2026-01-01T00:00:00Z',
      user: { username: 'alice', role: 'user', source: 'ldap' },
    };
    fetchMock.mockResolvedValue(mockResponse(200, payload));

    await expect(api.login('alice', 'secret')).resolves.toEqual(payload);
  });

  it('logout 发送已保存的会话令牌并接受 204', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'session-abc');
    fetchMock.mockResolvedValue({ ok: true, status: 204, json: vi.fn() } as unknown as Response);

    await expect(api.logout()).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/auth/logout`);
    expect(init.method).toBe('POST');
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer session-abc');
  });

  it('me / users / putQuota 使用约定的路径与方法', async () => {
    fetchMock.mockResolvedValue(mockResponse(200, {}));

    await api.me();
    await api.users();
    await api.putQuota('alice', { max_gpus: 4, max_active_jobs: null });

    const [meUrl, meInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(meUrl).toBe(`${API_BASE}/auth/me`);
    expect(meInit.method).toBeUndefined();

    const [usersUrl] = fetchMock.mock.calls[1] as [string];
    expect(usersUrl).toBe(`${API_BASE}/users`);

    const [quotaUrl, quotaInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(quotaUrl).toBe(`${API_BASE}/users/alice/quota`);
    expect(quotaInit.method).toBe('PUT');
    expect(quotaInit.body).toBe(JSON.stringify({ max_gpus: 4, max_active_jobs: null }));
  });

  it('用户名中的特殊字符在配额路径里被转义', async () => {
    fetchMock.mockResolvedValue(mockResponse(200, {}));

    await api.putQuota('alice@corp.com', { max_gpus: 2, max_active_jobs: 1 });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${API_BASE}/users/alice%40corp.com/quota`);
  });

  it('带令牌的 /auth/me 返回 401 时派发 UNAUTHORIZED_EVENT（会话过期自动跳登录）', async () => {
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-session');
    fetchMock.mockResolvedValue(mockResponse(401, { detail: 'Not authenticated' }));

    await expect(api.me()).rejects.toMatchObject({ status: 401, message: 'Not authenticated' });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer expired-session');
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it('/health 即使返回 401 也不会把用户踢出登录页', async () => {
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    fetchMock.mockResolvedValue(mockResponse(401, null));

    await expect(api.getHealth()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).not.toHaveBeenCalled();

    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it('403 使用权限相关的兜底文案', async () => {
    fetchMock.mockResolvedValue(mockResponse(403, null));

    await expect(api.users()).rejects.toMatchObject({
      status: 403,
      message: '当前账号没有执行该操作的权限',
    });
  });
});

describe('令牌存储', () => {
  it('静态令牌与会话令牌分开存放，请求优先使用会话令牌', () => {
    storeStaticToken('shared-token');
    expect(getStaticToken()).toBe('shared-token');
    expect(getStoredToken()).toBe('shared-token');

    storeSessionToken('session-token');
    expect(getSessionToken()).toBe('session-token');
    expect(getStoredToken()).toBe('session-token');

    // 重新录入静态令牌会清掉旧的 ldap 会话
    storeStaticToken('shared-token-2');
    expect(getSessionToken()).toBeNull();
    expect(getStoredToken()).toBe('shared-token-2');

    clearStoredTokens();
    expect(getStoredToken()).toBeNull();
  });

  it('isUnauthorized / isForbidden 只识别对应的状态码', () => {
    expect(isUnauthorized(new ApiError('x', 401))).toBe(true);
    expect(isUnauthorized(new ApiError('x', 403))).toBe(false);
    expect(isUnauthorized(new Error('x'))).toBe(false);
    expect(isForbidden(new ApiError('x', 403))).toBe(true);
    expect(isForbidden(null)).toBe(false);
  });
});
