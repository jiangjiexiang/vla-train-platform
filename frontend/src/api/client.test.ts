import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  API_BASE,
  ApiError,
  TOKEN_STORAGE_KEY,
  UNAUTHORIZED_EVENT,
  api,
  getErrorMessage,
  isAbortError,
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
