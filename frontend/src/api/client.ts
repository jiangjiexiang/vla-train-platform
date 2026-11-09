import type {
  ClusterSummary,
  HealthResponse,
  Job,
  JobEventsResponse,
  JobListResponse,
  JobLogs,
  PlatformConfig,
  QueueListResponse,
  SubmitJobRequest,
} from '../types/api';

export const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/+$/, '');
export const TOKEN_STORAGE_KEY = 'vla-platform-token';
export const UNAUTHORIZED_EVENT = 'vla-platform:unauthorized';

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

interface RequestOptions extends RequestInit {
  token?: string | null;
}

function errorDetail(payload: unknown, fallback: string): string {
  if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === 'object' && item !== null && 'msg' in item) {
            return String((item as { msg: unknown }).msg);
          }
          return String(item);
        })
        .join('；');
    }
  }
  return fallback;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { token: explicitToken, headers, ...init } = options;
  const token = explicitToken === undefined ? localStorage.getItem(TOKEN_STORAGE_KEY) : explicitToken;
  const requestHeaders = new Headers(headers);
  requestHeaders.set('Accept', 'application/json');
  if (init.body) requestHeaders.set('Content-Type', 'application/json');
  if (token) requestHeaders.set('Authorization', `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers: requestHeaders });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('无法连接 API 服务，请检查网络连接和后端服务状态', 0);
  }

  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // The status-specific message below is more useful than an empty response body.
    }
    if (response.status === 401 && token) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    const fallback = response.status === 401
      ? '访问令牌无效或已过期，请重新登录'
      : response.status === 404
        ? '请求的资源不存在'
        : response.status >= 500
          ? '集群服务暂时异常，请稍后重试或联系管理员'
          : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(errorDetail(payload, fallback), response.status);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  getHealth: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal, token: null }),
  getConfig: (signal?: AbortSignal, token?: string | null) =>
    request<PlatformConfig>('/config', { signal, token }),
  getClusterSummary: (signal?: AbortSignal) =>
    request<ClusterSummary>('/cluster/summary', { signal }),
  getJobs: (signal?: AbortSignal) => request<JobListResponse>('/jobs', { signal }),
  getJob: (name: string, signal?: AbortSignal) =>
    request<Job>(`/jobs/${encodeURIComponent(name)}`, { signal }),
  getJobEvents: (name: string, signal?: AbortSignal) =>
    request<JobEventsResponse>(`/jobs/${encodeURIComponent(name)}/events`, { signal }),
  submitJob: (data: SubmitJobRequest) =>
    request<Job>('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  cancelJob: (name: string) =>
    request<Job>(`/jobs/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  cloneJob: (name: string, newName: string) =>
    request<Job>(`/jobs/${encodeURIComponent(name)}/clone`, {
      method: 'POST',
      body: JSON.stringify({ name: newName }),
    }),
  getJobLogs: (name: string, tailLines = 500, signal?: AbortSignal) =>
    request<JobLogs>(`/jobs/${encodeURIComponent(name)}/logs?tail_lines=${tailLines}`, { signal }),
  getQueues: (signal?: AbortSignal) => request<QueueListResponse>('/queues', { signal }),
};

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return '发生未知错误，请稍后重试';
}
