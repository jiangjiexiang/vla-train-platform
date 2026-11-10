import type { ClusterSummary, CurrentUser, DatasetListResponse, DatasetMeta, HealthResponse, Job, JobDiagnosisResponse, JobEventsResponse, JobListResponse, JobLogs, JobMetricsResponse, JobStatus, JobTimelineResponse, JobCheckpointsResponse, LoginResponse, PlatformConfig, PreflightResponse, QuotaUpdate, QuotaUpdateResponse, QueueListResponse, Recipe, RecipeListResponse, RunRequest, SubmitJobRequest, UserListResponse, ImageListResponse, ImageParamsResponse,
  TensorboardResponse,
  FailureSummary,
} from '../types/api';

export const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/+$/, '');
// ldap 模式下的会话令牌
export const TOKEN_STORAGE_KEY = 'vla-platform-token';
// token 模式下的静态共享令牌，与登录会话分开存储，便于 /health 之后还原
export const STATIC_TOKEN_STORAGE_KEY = 'vla-platform-static-token';
export const UNAUTHORIZED_EVENT = 'vla-platform:unauthorized';

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

interface RequestOptions extends RequestInit {
  token?: string | null;
  // 登录类请求的 401 是「账号密码错误」，不应触发全局登录失效事件
  ignoreUnauthorized?: boolean;
}

// 任务列表的服务端筛选/分页参数；空字符串与 null 视为未传
export interface JobListParams {
  limit?: number | null;
  offset?: number | null;
  status?: JobStatus | null;
  q?: string | null;
}

function buildJobListQuery(params: JobListParams): string {
  const search = new URLSearchParams();
  if (params.limit !== undefined && params.limit !== null) {
    search.set('limit', String(params.limit));
  }
  if (params.offset !== undefined && params.offset !== null) {
    search.set('offset', String(params.offset));
  }
  if (params.status !== undefined && params.status !== null) {
    search.set('status', params.status);
  }
  const keyword = params.q?.trim();
  if (keyword) search.set('q', keyword);
  const query = search.toString();
  return query ? `?${query}` : '';
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

function statusFallback(status: number): string {
  if (status === 401) return '账号或密码错误，或登录已过期，请重新登录';
  if (status === 403) return '当前账号没有执行该操作的权限';
  if (status === 404) return '请求的资源不存在';
  if (status >= 500) return '集群服务暂时异常，请稍后重试或联系管理员';
  return `请求失败（HTTP ${status}）`;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { token: explicitToken, ignoreUnauthorized, headers, ...init } = options;
  const token = explicitToken === undefined ? getStoredToken() : explicitToken;
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
    // 只有确实带了令牌的 401 才代表登录失效；health 等匿名请求的 401 不应踢出登录页
    if (response.status === 401 && token && !ignoreUnauthorized) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(errorDetail(payload, statusFallback(response.status)), response.status);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  // 失败原因分布（仅管理员）。运维用它判断是不是平台在制造失败
  // 预估训练时长。按同 batch 的历史实测速度算，没有历史时用保守兜底值
  estimateRun: (payload: { recipe_id: string; dataset_id: string; overrides?: Record<string, unknown> }) =>
    request<{ estimated_seconds: number | null; estimated_text: string; range_text?: string; source: string; samples: number }>(
      '/runs/estimate',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  getFailureSummary: (signal?: AbortSignal) =>
    request<FailureSummary>('/jobs/failures/summary', { signal }),
  getHealth: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal, token: null, ignoreUnauthorized: true }),
  getConfig: (signal?: AbortSignal, token?: string | null) =>
    request<PlatformConfig>('/config', { signal, token }),
  // 提交页镜像候选：默认镜像 + 历史/运行中任务用过的镜像（这些镜像确定已存在节点上）
  getImages: (signal?: AbortSignal) => request<ImageListResponse>('/images', { signal }),
  // v0.9.1：镜像的训练参数模板。镜像名含 `/` 和 `:`，必须整体编码，
  // 否则 `vla-smolvla:latest` 里的 `:` 会被当成端口、`/` 会被当成路径分隔。
  getImageParams: (image: string, signal?: AbortSignal) =>
    request<ImageParamsResponse>(`/images/${encodeURIComponent(image)}/params`, { signal }),

  // ---- v0.9 鉴权 ----
  // 401 = 账号或密码错误；503 = LDAP 不可达（前端据此提示「认证服务不可用」而不是密码错误）
  login: (username: string, password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      token: null,
      ignoreUnauthorized: true,
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  me: (signal?: AbortSignal) => request<CurrentUser>('/auth/me', { signal }),

  // ---- v0.9 用户与配额（仅 admin）----
  users: (signal?: AbortSignal) => request<UserListResponse>('/users', { signal }),
  putQuota: (username: string, quota: QuotaUpdate) =>
    request<QuotaUpdateResponse>(`/users/${encodeURIComponent(username)}/quota`, {
      method: 'PUT',
      body: JSON.stringify(quota),
    }),

  getClusterSummary: (signal?: AbortSignal) =>
    request<ClusterSummary>('/cluster/summary', { signal }),
  getJobs: (params: JobListParams = {}, signal?: AbortSignal) =>
    request<JobListResponse>(`/jobs${buildJobListQuery(params)}`, { signal }),
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

  // ---- v1.0 数据集（只读；refresh=true 时后端先重扫有变化的目录）----
  listDatasets: (refresh = false, signal?: AbortSignal) =>
    request<DatasetListResponse>(`/datasets${refresh ? '?refresh=true' : ''}`, { signal }),
  // deep=true 会读 parquet 与视频目录，只在详情抽屉按需触发
  getDataset: (id: string, deep = false, signal?: AbortSignal) =>
    request<DatasetMeta>(`/datasets/${encodeURIComponent(id)}${deep ? '?deep=true' : ''}`, { signal }),

  // ---- v1.0 训练配方与一键训练 ----
  // 列表项已含展开后的 params，提交页据此渲染参数表单
  listRecipes: (signal?: AbortSignal) => request<RecipeListResponse>('/recipes', { signal }),
  getRecipe: (id: string, signal?: AbortSignal) =>
    request<Recipe>(`/recipes/${encodeURIComponent(id)}`, { signal }),
  // 提交前预检：不创建任务，返回能否提交与问题清单。ok=false 时 /runs 也会以 422 拒绝
  preflightRun: (payload: RunRequest) =>
    request<PreflightResponse>('/runs/preflight', { method: 'POST', body: JSON.stringify(payload) }),
  // 201 返回新建的 Job；422 = 格式不兼容 / 数据集不可用 / 参数未知，409 = 任务名重复
  createRun: (payload: RunRequest) =>
    request<Job>('/runs', { method: 'POST', body: JSON.stringify(payload) }),

  // 训练指标（loss 曲线）。老任务没有 metrics.jsonl 时返回空 items + path_exists=false
  getJobMetrics: (name: string, limit = 500, signal?: AbortSignal) =>
    request<JobMetricsResponse>(
      `/jobs/${encodeURIComponent(name)}/metrics?limit=${limit}`,
      { signal },
    ),
  // TensorBoard 曲线。events 文件不存在时返回 200 + path_exists=false；
  // 镜像里没装 tensorboard 时返回 503。详情页只在用户切到 TensorBoard 时才调用
  getJobTensorboard: (name: string, limit = 2000, signal?: AbortSignal) =>
    request<TensorboardResponse>(
      `/jobs/${encodeURIComponent(name)}/tensorboard?limit=${limit}`,
      { signal },
    ),
  // 失败诊断；任务没失败时 diagnosis 为 null
  getJobDiagnosis: (name: string, signal?: AbortSignal) =>
    request<JobDiagnosisResponse>(`/jobs/${encodeURIComponent(name)}/diagnosis`, { signal }),
  getJobTimeline: (name: string, signal?: AbortSignal) =>
    request<JobTimelineResponse>(`/jobs/${encodeURIComponent(name)}/timeline`, { signal }),
  getJobCheckpoints: (name: string, signal?: AbortSignal) =>
    request<JobCheckpointsResponse>(`/jobs/${encodeURIComponent(name)}/checkpoints`, { signal }),
};

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return '发生未知错误，请稍后重试';
}

// 当前应随请求发送的令牌：优先 ldap 会话令牌，其次 token 模式的静态令牌
export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || localStorage.getItem(STATIC_TOKEN_STORAGE_KEY);
}

export function getSessionToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function getStaticToken(): string | null {
  return localStorage.getItem(STATIC_TOKEN_STORAGE_KEY);
}

export function storeSessionToken(token: string) {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function storeStaticToken(token: string) {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  localStorage.setItem(STATIC_TOKEN_STORAGE_KEY, token);
}

export function clearStoredTokens() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  localStorage.removeItem(STATIC_TOKEN_STORAGE_KEY);
}