export type JobStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'
  | 'UNKNOWN';

export type Priority = 'low' | 'normal' | 'high';

// v0.9：鉴权模式（PLATFORM_AUTH_MODE），off 等价于旧版 PLATFORM_AUTH_REQUIRED=false
// 与后端 config.AUTH_MODES 保持一致。
// local = 平台自己维护的账号密码（docs/auth.md）
export type AuthMode = 'off' | 'token' | 'ldap' | 'local';

// v0.9：角色。admin 可管理用户与配额、查看所有用户的任务；user 只能操作自己的任务
export type UserRole = 'admin' | 'user';

export interface HealthResponse {
  status: string;
  auth_required?: boolean;
  // 后端可选返回；缺省时前端按旧版 auth_required 推断
  auth_mode?: AuthMode;
}

// null 表示不限
export interface UserQuota {
  max_gpus: number | null;
  max_active_jobs: number | null;
}

// GET /auth/me 返回的生效配额：比 /users 的 quota 额外带实时用量
export interface EffectiveQuota extends UserQuota {
  gpu_in_use?: number;
  active_jobs?: number;
}

export interface CurrentUser {
  username: string;
  role: UserRole;
  source: AuthMode | string;
  // 匿名模式示例：{"username": "anonymous", "role": "admin", "source": "off"}
  auth_mode?: AuthMode;
  quotas?: EffectiveQuota;
}

export interface UserSummary {
  username: string;
  role: UserRole | string;
  source: AuthMode | string;
  last_seen_at: string | null;
  active_jobs: number;
  gpu_in_use: number;
  quota: UserQuota;
}

export interface UserListResponse {
  items: UserSummary[];
}

export interface QuotaUpdate {
  max_gpus: number | null;
  max_active_jobs: number | null;
}

// PUT /users/{username}/quota 的响应
export interface QuotaUpdateResponse {
  username: string;
  quota: EffectiveQuota;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  token: string;
  expires_at: string;
  user: CurrentUser;
}

export interface SubmitJobRequest {
  name: string;
  image: string;
  gpus: number;
  queue: string;
  priority: Priority;
  command: string;
  env: Record<string, string>;
  max_retries: number;
  // 取消任务时留给训练进程退出的宽限秒数（1-600）
  termination_grace_period_seconds: number;
  // v0.9：自由标签，键值均须匹配 ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$，总长 <= 256 字节
  labels?: Record<string, string>;
  // v0.9：多机训练预留字段（nodes > 1 时 gpus = nodes * gpus_per_node）
  nodes?: number;
  gpus_per_node?: number;
}

export interface Job extends SubmitJobRequest {
  status: JobStatus;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  workload_status: string | null;
  pod_status: string | null;
  message: string | null;
  checkpoint_dir: string;
  // v0.9：提交者用户名；v0.8 老任务回填为空串
  owner?: string;
  // v0.9：后端会把 nodes 归一化为 1（v0.8 老任务缺省）
  nodes?: number;
  // v0.9：GPU 资源名，当前固定 nvidia.com/gpu
  gpu_type?: string;
  gpus_per_node?: number;
  // v1.0：排队位置与预估等待（非排队任务为 null）
  queue_position?: number | null;
  queue_eta_seconds?: number | null;
  // 估算依据；default 表示没有历史数据，前端要显示「约」
  queue_eta_source?: 'recipe' | 'history' | 'default' | string | null;
  queue_state?: QueueState | string;
}

export interface JobListResponse {
  items: Job[];
  total: number;
  // 本次请求实际使用的分页参数；不传分页参数时后端可能返回全部任务，故 limit 可为空
  limit?: number | null;
  offset?: number;
}

export interface PlatformConfig {
  allowed_gpus: number[];
  default_image: string;
  default_command: string;
  default_queue: string;
  priorities: Priority[];
  dataset_path: string;
  checkpoint_root: string;
  // v0.9：PLATFORM_MAX_NODES，默认 1；仅该值 > 1 时前端展示多机选项
  max_nodes?: number;
  // v0.9：当前鉴权模式，便于登录页区分「访问令牌」与「账号密码」
  auth_mode?: AuthMode;
}

export interface ClusterNode {
  name: string;
  ready: boolean;
  gpu_total: number;
  gpu_allocated: number;
}

export interface GpuMetric {
  index: number;
  name: string;
  utilization_percent: number;
  memory_used_mib: number;
  memory_total_mib: number;
  temperature_celsius: number;
}

export interface ClusterSummary {
  gpu_total: number;
  gpu_allocated: number;
  gpu_available: number;
  node_count: number;
  nodes: ClusterNode[];
  gpu_metrics: {
    available: boolean;
    items: GpuMetric[];
  };
  jobs: {
    queued: number;
    running: number;
    succeeded: number;
    failed: number;
    cancelled: number;
    unknown?: number;
  };
}

export interface QueueItem {
  name: string;
  job_name?: string;
  priority: Priority | string;
  status: JobStatus | string;
  created_at: string | null;
  // v0.9：任务提交者（老任务可能缺省）
  owner?: string;
}

export interface Queue {
  name: string;
  cluster_queue: string;
  gpu_quota: number;
  gpu_used: number;
  pending: number;
  admitted: number;
  items: QueueItem[];
}

export interface QueueListResponse {
  items: Queue[];
}

export interface JobLogs {
  name: string;
  pod_name: string | null;
  logs: string;
}

export interface JobEvent {
  type: string | null;
  reason: string | null;
  message: string | null;
  count: number;
  object_kind: string | null;
  object_name: string | null;
  first_seen: string | null;
  last_seen: string | null;
}

export interface JobEventsResponse {
  name: string;
  items: JobEvent[];
}

/** 提交页镜像下拉的一项。sources 说明该镜像的来源：default / running / history。 */
export interface ImageOption {
  image: string;
  sources: string[];
}

export interface ImageListResponse {
  items: ImageOption[];
}

// ---- v0.9.1：训练参数模板（见 .agents/PARAMS-TEMPLATE-CONTRACT.md 第 1、2 节）----

export type ParamType = 'int' | 'float' | 'str' | 'bool' | 'enum' | 'path';

export interface ParamSpec {
  key: string;
  // 命令行 flag，形如 --steps。内置模板里是 extra_args 那种「透传」参数时可能为 null
  flag: string | null;
  label: string;
  type: ParamType;
  default?: unknown;
  min?: number;
  max?: number;
  required?: boolean;
  help?: string;
  placeholder?: string;
  // enum 的候选值
  options?: string[];
  // 仅 bool 可用：为字符串时拼 `flag + " " + value`，否则只拼 flag（true）/跳过（false）
  true_value?: string | null;
  false_value?: string | null;
  // 仅内置模板的兼容写法：kind === 'extra' 表示该参数的值原样追加到命令末尾
  kind?: string;
}

export interface ExtraArgsSpec {
  label?: string;
  help?: string;
  enabled?: boolean;
  default?: string;
}

export interface ParamsTemplate {
  schema_version: number;
  entrypoint?: string | null;
  params: ParamSpec[];
  extra_args?: ExtraArgsSpec;
}

export interface ImageParamsResponse {
  image: string;
  // file = 镜像登记的模板；builtin = 平台通用模板；error = 读取失败后回退到 builtin
  source: 'file' | 'builtin' | 'error';
  warning?: string;
  template: ParamsTemplate;
}

// ---- v1.0：数据集（见 backend/app/datasets/scanner.py DatasetMeta.as_dict）----

// scanning 是扫描进行中的临时状态；error 表示扫描本身失败
export type DatasetStatus = 'scanning' | 'ready' | 'warning' | 'unusable' | 'error';

export type DatasetIssueLevel = 'error' | 'warning' | 'info';

export interface DatasetIssue {
  level: DatasetIssueLevel | string;
  code: string;
  message: string;
}

export interface DatasetMeta {
  id: string;
  name: string;
  // 容器内路径，如 /datasets/pick1028
  path: string;
  // 数据集格式，如 lerobot-v3.0；扫描失败时可能是 unknown
  format: string;
  status: DatasetStatus | string;
  size_bytes: number;
  episodes: number;
  frames: number;
  fps: number;
  tasks: string[];
  robot_type: string | null;
  // 相机名列表，如 ["observation.images.top"]
  cameras: string[];
  // [height, width]，浅扫描拿不到时为 null
  image_size: number[] | null;
  state_dim: number;
  action_dim: number;
  action_names: string[];
  state_names: string[];
  duration_seconds: number;
  issues: DatasetIssue[];
  scanned_at: string;
  scan_depth: 'shallow' | 'deep' | string;
  dir_mtime: number;
}

export interface DatasetSummary {
  total: number;
  ready: number;
  warning: number;
  unusable: number;
  total_frames: number;
}

export interface DatasetListResponse {
  items: DatasetMeta[];
  summary: DatasetSummary;
  root: string;
}

// ---- v1.0：训练配方（见 backend/app/recipes/loader.py + routes_runs._recipe_summary）----

export interface Recipe {
  id: string;
  name: string;
  description: string;
  // 训练镜像；配置解析失败的配方是空串
  image: string;
  entrypoint: string | null;
  // 支持的数据集格式，与 DatasetMeta.format 精确匹配
  dataset_formats: string[];
  params_template: string | null;
  // 默认参数取值（只对模板里声明过的 key 生效）
  defaults: Record<string, unknown>;
  gpu_count: number;
  estimated_vram_gb: number | null;
  env: Record<string, string>;
  order: number;
  // false 只在高级提交页可见
  public: boolean;
  // 内联模板（template 字段）与 params_template 二选一
  has_custom_template?: boolean;
  // 列表接口已展开参数，渲染表单不必再请求一次
  params: ParamSpec[];
  extra_args: ExtraArgsSpec | null;
}

export interface RecipeListResponse {
  items: Recipe[];
}

// POST /runs 的请求体（见 backend/app/models.py RunSubmitRequest）
export interface RunRequest {
  recipe_id: string;
  dataset_id: string;
  // 留空（不传）由后端生成 <recipe>-<dataset>-<序号>
  name?: string | null;
  // 键名是 ParamSpec.key；未知 key 会被后端以 422 拒绝
  overrides?: Record<string, unknown>;
  priority?: Priority;
  env?: Record<string, string>;
}

// ---- v1.0：提交前预检（backend/app/services/preflight.py）----

export interface PreflightCheck {
  level: 'error' | 'warning' | 'info' | string;
  code: string;
  message: string;
  // 可执行的修复建议，前端必须展示
  suggestion: string;
}

export interface PreflightResponse {
  // false 表示有 error，后端会拒绝提交
  ok: boolean;
  checks: PreflightCheck[];
  summary: { errors: number; warnings: number };
}

// ---- v1.0：排队位置与 ETA（backend/app/services/queue_est.py）----

export type QueueState = 'queued' | 'admitted' | 'running' | 'finished';

// ---- v1.0：训练指标与失败诊断 ----

export interface MetricPoint {
  step: number;
  // 其余是训练脚本写入的数值字段（loss / lr / epoch ...）
  [key: string]: number | string | undefined;
}

export interface JobMetricsResponse {
  name: string;
  items: MetricPoint[];
  total_points: number;
  truncated: boolean;
  // false = 还没有 metrics.jsonl（老任务或刚开始的任务），不是错误
  path_exists: boolean;
}

export interface JobDiagnosis {
  code: string;
  summary: string;
  suggestion: string;
  evidence: string;
}

export interface JobDiagnosisResponse {
  name: string;
  status: JobStatus | string;
  // 任务没失败时为 null
  diagnosis: JobDiagnosis | null;
}

// ---- v1.0：作业阶段时间轴（backend/app/services/timeline.py）----
export type TimelineStageState = 'done' | 'current' | 'pending' | 'failed';

export interface TimelineItem {
  stage: string;
  label: string;
  state: TimelineStageState;
  // 取不到时间戳时为 null，前端显示成「—」而不是编造时间
  at: string | null;
}

export interface JobTimelineResponse {
  name: string;
  status: JobStatus | string;
  // 已结束的任务为 null：所有阶段都走完了，没有「当前阶段」
  current_stage: string | null;
  items: TimelineItem[];
}

// ---- v1.0：训练产物（backend/app/services/checkpoints.py）----
export interface CheckpointFile {
  name: string;
  size_bytes: number;
  modified_at: string | null;
  // true 表示模型权重；metrics.jsonl 这类产物为 false
  is_weights: boolean;
}

export interface JobCheckpointsResponse {
  name: string;
  exists: boolean;
  path: string;
  total_bytes: number;
  items: CheckpointFile[];
  truncated: boolean;
}

/** 失败原因聚合里的一项。 */
export interface FailureReason {
  code: string;
  count: number;
  summary?: string | null;
  suggestion?: string | null;
  /** 最近一个该原因的失败任务，点进去能看日志 */
  example_job?: string;
}

export interface FailureSummary {
  total_failed: number;
  classified: number;
  unknown: number;
  reasons: FailureReason[];
}
