# Cross-module Contracts

This file is the integration contract for all implementation agents. Read `PLAN.md` for scope. Keep the first version small and do not add out-of-scope services.

## Source tree and ownership

- Local source of truth: `/home/jjx/vla-train-platform`.
- Cluster agent: `deploy/`, `scripts/preflight.sh`, `scripts/install-k3s-gpu.sh`, `scripts/install-stack.sh`, `scripts/deploy-web.sh`.
- Backend agent: `backend/` only.
- Frontend agent: `frontend/` only.
- Training assets agent: `cli/`, `images/`, `examples/` only.
- Verification/docs agent: `README.md`, `docs/`, `scripts/verify-4090.sh`, `scripts/verify-a100.sh` only.
- Main agent: integration fixes, root build files, remote synchronization and acceptance.

Do not modify files owned by another agent. Report integration issues instead.

## Runtime topology

- Single-node K3s for both RTX 4090 and single-node 8xA100.
- Web system namespace: `vla-platform`.
- Training namespace: `vla-training`.
- Kueue LocalQueue: `default`.
- Kueue ClusterQueue: `vla-cluster-queue`.
- Kueue ResourceFlavor: `gpu`.
- Kueue WorkloadPriorityClass values: `low`, `normal`, `high`.
- Kueue API version: `kueue.x-k8s.io/v1beta2` (Kueue v0.20.5).
- Training jobs are single-node PyTorchJobs with one Master replica. Worker is omitted in v1.
- Training Pods use `runtimeClassName: nvidia`; the cluster-wide default runtime does not need to be changed.
- PyTorchJob container name: `pytorch`.
- Web is one container: FastAPI serves the compiled React SPA and `/api/v1`.
- No external registry is required for smoke tests; document how images are imported into K3s/containerd.

## Host and container paths

- Host datasets: `/data/vla-platform/datasets` -> container `/datasets` (read-only).
- Host checkpoints: `/data/vla-platform/checkpoints` -> container `/checkpoints` (read-write).
- Each job checkpoint directory: `/checkpoints/<job-name>`.
- Users cannot supply arbitrary host paths through API or CLI.
- Web history database defaults to `/data/history/platform.db` inside the web container and must be persisted by a volume.

## Job naming and metadata

Job names must match `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, maximum length 50. Reject uppercase, underscores and an existing live job with the same name.

Common labels:

```text
app.kubernetes.io/name=vla-train-platform
app.kubernetes.io/managed-by=vla-train-platform
vla-platform/job-name=<job-name>
kueue.x-k8s.io/queue-name=default
kueue.x-k8s.io/priority-class=<low|normal|high>
```

Common annotations:

```text
vla-platform/submitted-at=<RFC3339 UTC>
vla-platform/spec=<compact JSON of original submit spec>
```

GPU requests and limits must both be set to the same integer `nvidia.com/gpu` value so Kueue accounting works.

## Environment variables

Backend/web:

```text
PLATFORM_API_TOKEN                  required when authentication is enabled; static bearer token
PLATFORM_AUTH_REQUIRED              default true; current 4090 overlay sets false
PLATFORM_NAMESPACE                  default vla-training
PLATFORM_QUEUE                      default default
PLATFORM_ALLOWED_GPUS               4090: "1"; A100: "1,2,4,8"
PLATFORM_DEFAULT_IMAGE              default vla-smoke:latest
PLATFORM_DEFAULT_COMMAND            default python /app/train.py
PLATFORM_DATASET_HOST_PATH          default /data/vla-platform/datasets
PLATFORM_CHECKPOINT_HOST_PATH       default /data/vla-platform/checkpoints
PLATFORM_DB_PATH                    default /data/history/platform.db
PLATFORM_KUBECONFIG                 optional; in-cluster config is default in Deployment
PLATFORM_LOG_TAIL_DEFAULT           default 500
PLATFORM_GPU_METRICS_URL            default http://vla-gpu-metrics.vla-platform.svc:8080/; empty disables metrics
```

Frontend build/runtime:

```text
VITE_API_BASE                       build-time optional; default /api/v1
```

When authentication is enabled, the browser uses `Authorization: Bearer <token>` and all endpoints except health require the token. When `PLATFORM_AUTH_REQUIRED=false`, the UI skips login and API requests do not need a token. The health response includes the boolean `auth_required` so the UI can discover the mode.

## Submit model

`POST /api/v1/jobs` request:

```json
{
  "name": "exp001",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 100",
  "env": {"PYTHONUNBUFFERED": "1"},
  "max_retries": 0
}
```

Rules:

- `name`, `image`, `gpus`, and `command` are required except where a documented default is applied.
- `queue` must be `default`; `priority` is low/normal/high.
- `gpus` must be in `PLATFORM_ALLOWED_GPUS`.
- `env` keys must be valid environment variable names. Values are strings.
- `max_retries` is 0, 1, or 2. Zero maps to restartPolicy Never; positive maps to OnFailure.
- `command` is capped at 65536 bytes and the summed `env` entries at 65536 bytes, because the whole spec is
  stored in a Kubernetes annotation and annotations are capped at 256 KiB. These caps are enforced on submit
  only, never when reading an object or history back, so older oversized jobs stay readable and cancellable.
- Command is an intentional shell command executed with `/bin/bash -lc`; do not allow extra volumes, privileged mode, or arbitrary security context.
- The original validated request is stored in the job annotation and SQLite.

## Job response model

```json
{
  "name": "exp001",
  "status": "QUEUED",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 100",
  "env": {"PYTHONUNBUFFERED": "1"},
  "max_retries": 0,
  "created_at": "2026-01-01T00:00:00Z",
  "started_at": null,
  "finished_at": null,
  "duration_seconds": null,
  "workload_status": "Pending",
  "pod_status": null,
  "message": "Waiting for Kueue admission",
  "checkpoint_dir": "/checkpoints/exp001"
}
```

Statuses are exactly `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `UNKNOWN`.

- Kueue not admitted or no training pod yet: QUEUED.
- Training pod running or PyTorchJob Running: RUNNING.
- PyTorchJob Succeeded/Failed: SUCCEEDED/FAILED.
- Cancelled records come from SQLite after the Kubernetes job is deleted.
- K8s is the live state source; SQLite never decides scheduling state.

`GET /api/v1/jobs` returns `{ "items": [<job>], "total": 1 }`, newest first, including live jobs and cancelled history.

`GET /api/v1/jobs/{name}` returns one job. Missing live/history job returns 404.

`DELETE /api/v1/jobs/{name}` deletes the PyTorchJob, releasing resources. A queued or running job is recorded
in history as `CANCELLED` with message `Cancelled by user`. A job that already finished keeps its real
terminal status (`SUCCEEDED` / `FAILED`) with message `Deleted after the job finished with <STATUS>`, so
deleting a completed run never rewrites it as cancelled. It returns that history record; repeated deletion
returns the same record without error.

`POST /api/v1/jobs/{name}/clone` accepts `{ "name": "exp002" }`, validates the new name, copies the original submit model, and returns the new job.

## Other API contracts

- `GET /api/v1/health` -> `{ "status": "ok", "auth_required": false }`; `auth_required` reflects the configured authentication mode.
- `GET /api/v1/config`:

```json
{
  "allowed_gpus": [1],
  "default_image": "vla-smoke:latest",
  "default_command": "python /app/train.py",
  "default_queue": "default",
  "priorities": ["low", "normal", "high"],
  "dataset_path": "/datasets",
  "checkpoint_root": "/checkpoints"
}
```

- `GET /api/v1/cluster/summary`:

```json
{
  "gpu_total": 1,
  "gpu_allocated": 0,
  "gpu_available": 1,
  "node_count": 1,
  "nodes": [{"name":"node1","ready":true,"gpu_total":1,"gpu_allocated":0}],
  "gpu_metrics": {"available":true,"items":[{"index":0,"name":"NVIDIA GeForce RTX 4090","utilization_percent":0,"memory_used_mib":0,"memory_total_mib":49140,"temperature_celsius":30}]},
  "jobs": {"queued":0,"running":0,"succeeded":0,"failed":0,"cancelled":0}
}
```

`gpu_metrics` is scraped from the `vla-gpu-metrics` DaemonSet (nvidia-smi over HTTP), so it reports host-level
utilisation and memory that Kubernetes cannot see. If that service is unreachable the field degrades to
`{"available": false, "items": []}` instead of failing the whole summary.

- `GET /api/v1/queues` -> `{ "items": [{"name":"default","cluster_queue":"vla-cluster-queue","gpu_quota":1,"gpu_used":0,"pending":0,"admitted":0,"items":[]}] }`. Queue item objects include job name, priority, status and creation time.
- `GET /api/v1/jobs/{name}/logs?tail_lines=500` -> `{ "name":"exp001", "pod_name":"exp001-master-0", "logs":"..." }`. If no pod exists yet, return 200 with empty logs and `pod_name: null`.
- `GET /api/v1/jobs/{name}/events` -> events of the PyTorchJob and its training Pods:

```json
{
  "name": "exp001",
  "items": [
    {
      "type": "Warning",
      "reason": "Failed",
      "message": "Failed to pull image \"vla-smoke:latest\": ErrImagePull",
      "count": 3,
      "object_kind": "Pod",
      "object_name": "exp001-master-0",
      "first_seen": "2026-01-01T00:00:05Z",
      "last_seen": "2026-01-01T00:01:05Z"
    }
  ]
}
```

`items` is sorted by `last_seen` descending. Timestamps are RFC3339 UTC strings with second precision and a `Z` suffix, so lexical order equals chronological order; an item with no `last_seen` sorts last, and `first_seen`/`last_seen` may be `null` when the underlying event lacks the timestamp. `count` defaults to 1 when the event reports no count. Events are queried in `PLATFORM_NAMESPACE` for `involvedObject.name` equal to the job name or one of its Pod names, then de-duplicated by event UID; events owned by the Kueue `Workload` object are therefore not part of this response. A name that is neither a live job nor a cancelled history record returns 404 (`{ "detail": "job <name> not found" }`). A cancelled job whose PyTorchJob and Pods are already deleted returns 200 with `"items": []`, and a live job that has not produced events yet also returns 200 with an empty `items` list.

- Errors use FastAPI's standard `{ "detail": "message" }` shape.

## CLI contract

Executable: `cli/vla-train`.

Commands:

```text
vla-train config --server http://HOST:30080 --token TOKEN
vla-train submit --name exp001 --gpus 1 --image vla-smoke:latest --priority normal --command 'python /app/train.py'
vla-train list
vla-train status
vla-train logs exp001
vla-train events exp001 [--json] [省略名称列出全部]
vla-train cancel exp001
vla-train clone exp001 --name exp002
```

CLI talks to the REST API, not directly to Kubernetes. Use Python standard library where practical.
`events` renders last_seen / type / reason / object and the message by default; `--json` prints the raw
`/jobs/{name}/events` payload for scripting. Configuration is stored with mode 0600 in the user configuration directory; never hard-code a token.

## Smoke training assets

- Image name: `vla-smoke:latest`.
- Container entry training path: `/app/train.py`.
- Default single-GPU command: `python /app/train.py`.
- A100 8-GPU example command: `torchrun --standalone --nproc_per_node=8 /app/train.py --distributed`.
- Training prints clear CUDA/GPU, progress, and completion messages and writes a small checkpoint under the path given by `CHECKPOINT_DIR` or `/checkpoints/<job-name>`.
- Support configurable duration/steps so queue tests are deterministic.
- No large model download and no claim that smoke training is a real VLA model.

## Web UX

Pages/routes:

```text
/login
/dashboard
/submit
/jobs
/jobs/:name
/queues
```

- 3-5 second polling for dashboard, jobs, details and queues.
- Log view polls and preserves scroll-at-bottom behavior.
- Confirm cancellation with a dialog.
- Clone opens submit form populated from the source job.
- Forms use `GET /api/v1/config`; GPU selector only offers allowed values.
- Display API errors instead of silently succeeding.
- Responsive enough for normal desktop use; no mobile-specific work.

## Deployment and verification

- 4090 Kueue GPU quota is 1 and allowed GPU values are [1].
- A100 Kueue GPU quota is 8 and allowed GPU values are [1,2,4,8].
- Web NodePort defaults to 30080.
- Kueue must be installed with the `kubeflow.org/pytorchjob` integration enabled. The Kueue v0.20.5 Helm chart defaults and the official release `manifests.yaml` both comment that framework out and enable only `pod`, which silently degrades queueing to Pod granularity: the `Workload` is then named `pod-<pod>-<hash>` and owned by the Pod instead of the PyTorchJob. Install or upgrade Kueue with `helm ... -f deploy/kueue/base/kueue-values.yaml` (chart defaults with only that line changed); `kubectl apply -k deploy/kueue/<profile>/` applies queues and priorities only and does not touch Kueue's own configuration.
- Scripts use `set -euo pipefail`, print risky changes, and do not uninstall or overwrite an existing Docker/Kubernetes setup.
- Installation scripts are intended to be run on the target machine; they must not embed SSH credentials.
- Verification scripts must fail on unmet criteria and print clear next diagnostics.
- Documentation distinguishes local/static validation from real GPU acceptance.
