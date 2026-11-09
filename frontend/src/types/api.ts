export type JobStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'
  | 'UNKNOWN';

export type Priority = 'low' | 'normal' | 'high';

export interface HealthResponse {
  status: string;
  auth_required?: boolean;
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
}

export interface JobListResponse {
  items: Job[];
  total: number;
}

export interface PlatformConfig {
  allowed_gpus: number[];
  default_image: string;
  default_command: string;
  default_queue: string;
  priorities: Priority[];
  dataset_path: string;
  checkpoint_root: string;
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
