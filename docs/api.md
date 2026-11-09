# VLA Train Platform 接口参考

面向调用方（CLI、前端、脚本）的 HTTP API 说明。字段名与类型以线上 `GET /openapi.json` 和 `backend/app/api/routes.py` 为准；示例响应取自 4090 overlay 的真实返回（校验范围见 [§10](#10-附录文档来源与校验)）。

- 仓库版本：`VERSION` = `0.5.0`
- OpenAPI 文档版本：`info.version` = `1.0.0`（FastAPI 应用内声明）
- 接口前缀：`/api/v1`

---

## 1. 概述

### 1.1 Base URL

| 项 | 值 |
| --- | --- |
| Base URL | `http://<host>:30080/api/v1` |
| 4090 单机 overlay | `http://127.0.0.1:30080/api/v1` |
| 原始 OpenAPI JSON | `http://<host>:30080/openapi.json` |
| Swagger UI | `http://<host>:30080/docs` |

注意：`/openapi.json` 与 `/docs` 挂在站点根路径，**不在** `/api/v1` 下；`/api/v1/openapi.json` 会返回 404。30080 端口同时托管前端 SPA，非 `/api/` 路径会回落到前端静态资源。

### 1.2 鉴权

除 `/health` 外的所有端点都要求 `Authorization: Bearer <token>`。

| 项 | 说明 |
| --- | --- |
| 请求头 | `Authorization: Bearer <token>` |
| Token 来源 | 服务端环境变量 `PLATFORM_API_TOKEN` |
| 开关 | `PLATFORM_AUTH_REQUIRED`（默认 `true`）；为 `true` 时 `PLATFORM_API_TOKEN` 不得为空，否则进程启动失败 |
| 校验方式 | 常量时间比较（`hmac.compare_digest`） |
| 当前 4090 overlay | **鉴权关闭**：`GET /health` 返回 `"auth_required": false`，所有端点都不校验请求头，带了也不会报错 |

鉴权开启时的失败响应：

| 场景 | 状态码 | 响应体 | 响应头 |
| --- | --- | --- | --- |
| 未带 Token，或 scheme 不是 `bearer` | 401 | `{"detail":"Bearer token required"}` | `WWW-Authenticate: Bearer` |
| Token 不匹配 | 401 | `{"detail":"Invalid bearer token"}` | `WWW-Authenticate: Bearer` |

> 当前部署鉴权关闭，以上 401 行为依据 `backend/app/auth.py`（未实测，**待确认**实际网关是否会先行拦截）。

### 1.3 错误格式

所有错误统一为 `{"detail": ...}`，`detail` 有两种形态：

**A. 业务错误：字符串**（`ServiceError` / `HTTPException`）

```json
{"detail": "job nope-does-not-exist not found"}
```

**B. 请求校验错误：数组**（FastAPI / Pydantic，状态码 422）

```json
{
  "detail": [
    {
      "type": "string_pattern_mismatch",
      "loc": ["path", "name"],
      "msg": "String should match pattern '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'",
      "input": "Bad_Name",
      "ctx": {"pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}
    }
  ]
}
```

数组项的 `loc` / `type` / `msg` 必有，`input`、`ctx` 视错误类型出现（`loc` 首项为 `body`、`path` 或 `query`）。

未捕获异常统一返回 `500 {"detail":"internal server error"}`；`/api/v1` 下未注册的路径返回 `404 {"detail":"Not Found"}`。

### 1.4 通用约定

| 约定 | 说明 |
| --- | --- |
| 编码 | JSON，UTF-8；POST 请求需 `Content-Type: application/json` |
| 时间 | UTC，ISO 8601，秒级精度，固定 `YYYY-MM-DDTHH:MM:SSZ`（如 `2026-11-09T08:18:55Z`） |
| 任务名 | 1–50 字符，仅小写字母、数字和内部连字符，且必须以字母或数字开头/结尾（`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`） |
| 未知字段 | 请求体为 `extra="forbid"`：多传字段直接 422 |
| 列表与分页 | `GET /jobs` 支持 `limit` / `offset` / `status` / `q`；默认不分页、全量返回 |
| 布尔与 null | 字段可为 `null` 时按 `string \| null` 标注 |

---

## 2. 端点总览

| 方法 | 路径 | 用途 | 成功状态码 | 鉴权 |
| --- | --- | --- | --- | --- |
| GET | `/health` | 存活与鉴权模式探测 | 200 | 否 |
| GET | `/config` | 平台默认配置（镜像、命令、GPU、优先级） | 200 | 是 |
| GET | `/jobs` | 任务列表（含已取消历史，支持过滤/分页） | 200 | 是 |
| POST | `/jobs` | 提交训练任务 | 201 | 是 |
| GET | `/jobs/{name}` | 单个任务详情 | 200 | 是 |
| DELETE | `/jobs/{name}` | 取消任务并删除工作负载 | 200 | 是 |
| POST | `/jobs/{name}/clone` | 按已有任务配置克隆新任务 | 201 | 是 |
| GET | `/jobs/{name}/logs` | 读取训练容器日志 | 200 | 是 |
| GET | `/jobs/{name}/events` | 读取任务与 Pod 的 Kubernetes 事件 | 200 | 是 |
| GET | `/queues` | Kueue 队列配额与队列内任务 | 200 | 是 |
| GET | `/cluster/summary` | 集群 GPU 汇总与任务计数 | 200 | 是 |

---

## 3. 任务对象（Job）

`GET /jobs` 的数组元素、`GET /jobs/{name}`、`POST /jobs`、`POST /jobs/{name}/clone`、`DELETE /jobs/{name}` 返回的都是同一个对象结构。

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 任务名 |
| `status` | string | 否 | `QUEUED` / `RUNNING` / `SUCCEEDED` / `FAILED` / `CANCELLED`，判定规则见 [§7](#7-任务状态机) |
| `image` | string | 否 | 容器镜像（提交时未指定则为平台默认镜像） |
| `gpus` | integer | 否 | 申请的 GPU 数 |
| `queue` | string | 否 | Kueue LocalQueue 名 |
| `priority` | string | 否 | `low` / `normal` / `high` |
| `command` | string | 否 | 实际执行的 shell 命令（未指定则为平台默认命令） |
| `env` | object(string→string) | 否 | 用户环境变量；不含平台注入的 `CHECKPOINT_DIR` |
| `max_retries` | integer | 否 | `0` / `1` / `2` |
| `termination_grace_period_seconds` | integer | 否 | 取消后容器的优雅退出时长，1–600 |
| `created_at` | string | 否 | 提交时间（优先取注解 `vla-platform/submitted-at`） |
| `started_at` | string | **是** | 启动时间；未启动或取消历史记录为 `null` |
| `finished_at` | string | **是** | 终态时间；未结束为 `null`；取消记录为取消时间 |
| `duration_seconds` | integer | **是** | 运行时长；未启动、`QUEUED` 或已取消的历史记录为 `null` |
| `workload_status` | string | 否 | Kueue Workload 状态：`Pending` / `QuotaReserved` / `Admitted` / `PodsReady` / `Finished` / `Cancelled` |
| `pod_status` | string | **是** | Kubernetes Pod phase：`Pending` / `Running` / `Succeeded` / `Failed` / `Unknown`；无 Pod 或取消历史为 `null` |
| `message` | string | 否 | 人类可读状态说明，如 `Cancelled by user`、`PyTorchJob qb is successfully completed.` |
| `checkpoint_dir` | string | 否 | 固定为 `/checkpoints/<name>`，容器内对应可写挂载点 |

补充说明：

- `env` 里若客户端传入 `CHECKPOINT_DIR`，会被平台覆盖为 `/checkpoints/<name>`，不会出现在响应的 `env` 中。
- 由取消历史合成的对象（Kubernetes 对象已删除）固定 `started_at: null`、`duration_seconds: null`、`pod_status: null`。
- `duration_seconds`：`RUNNING` 时为「当前时间 − `started_at`」，终态时为「`finished_at` − `started_at`」，秒级取整；`started_at` 为 `null` 或状态为 `QUEUED` 时为 `null`。
- 删除一个**已终态**任务时，历史记录保留原状态（`SUCCEEDED` / `FAILED`），此时 `message` 为 `Deleted after the job finished with SUCCEEDED`，`workload_status` 为 `Finished`。

---

## 4. 系统端点

### 4.1 GET /health

**用途**：存活探测，并返回当前是否强制鉴权。无参数、无请求体、不需要 Token。

**响应 200**（实测）：

```json
{
  "status": "ok",
  "auth_required": false
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 固定 `ok` |
| `auth_required` | boolean | `true` 表示除本接口外都需要 Bearer Token |

**常见错误**：无业务错误；进程异常时由入口返回 500。

---

### 4.2 GET /config

**用途**：读取平台默认配置，供客户端在提交前展示可选值、填充缺省字段。

**查询参数**：无。

**响应 200**（实测）：

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

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `allowed_gpus` | array(integer) | 允许申请的 GPU 数集合，提交时 `gpus` 必须命中 |
| `default_image` | string | `image` 缺省值 |
| `default_command` | string | `command` 缺省值 |
| `default_queue` | string | 唯一可用队列名 |
| `priorities` | array(string) | 允许的优先级枚举 |
| `dataset_path` | string | 容器内只读数据集挂载点，固定 `/datasets` |
| `checkpoint_root` | string | 容器内可写检查点根目录，固定 `/checkpoints` |

**常见错误**

| 状态码 | 场景 |
| --- | --- |
| 401 | 鉴权开启且 Token 缺失/错误 |

---

## 5. 任务端点

### 5.1 GET /jobs

**用途**：列出训练任务（活跃任务 + 已取消的历史记录），按 `created_at` 倒序；支持按状态过滤、按名称搜索和分页。

**查询参数**（均可选）

| 参数 | 类型 | 必填 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `limit` | integer | 否 | 不传则返回全部 | 1–200 | 每页条数；不传时分页关闭，`offset` 被忽略 |
| `offset` | integer | 否 | `0` | ≥ 0 | 偏移量，仅在传 `limit` 时生效 |
| `status` | string | 否 | 不过滤 | `QUEUED` / `RUNNING` / `SUCCEEDED` / `FAILED` / `CANCELLED` | 精确匹配，**区分大小写**（小写会 422） |
| `q` | string | 否 | 不过滤 | ≤ 100 字符 | 任务名子串匹配，大小写不敏感（`q=QB` 可命中 `qb`） |

处理顺序：先按 `status`、`q` 过滤（同时传时取交集）→ 计算 `total` → 按 `offset` / `limit` 切片。因此 `total` 是**过滤后、分页前**的总数，`items` 长度 ≤ `limit`。

**响应 200**（实测 `GET /jobs?limit=2`）：

```json
{
  "items": [
    {
      "name": "gb",
      "status": "SUCCEEDED",
      "image": "vla-smoke:latest",
      "gpus": 1,
      "queue": "default",
      "priority": "normal",
      "command": "python /app/train.py --steps 3 --sleep 0",
      "env": {},
      "max_retries": 0,
      "termination_grace_period_seconds": 30,
      "created_at": "2026-11-09T08:45:12Z",
      "started_at": null,
      "finished_at": "2026-11-09T08:45:24Z",
      "duration_seconds": null,
      "workload_status": "Finished",
      "pod_status": null,
      "message": "Deleted after the job finished with SUCCEEDED",
      "checkpoint_dir": "/checkpoints/gb"
    },
    {
      "name": "ga",
      "status": "CANCELLED",
      "image": "vla-smoke:latest",
      "gpus": 1,
      "queue": "default",
      "priority": "normal",
      "command": "python /app/train.py --steps 60 --sleep 0.5",
      "env": {},
      "max_retries": 0,
      "termination_grace_period_seconds": 5,
      "created_at": "2026-11-09T08:45:11Z",
      "started_at": null,
      "finished_at": "2026-11-09T08:45:12Z",
      "duration_seconds": null,
      "workload_status": "Cancelled",
      "pod_status": null,
      "message": "Cancelled by user",
      "checkpoint_dir": "/checkpoints/ga"
    }
  ],
  "total": 22,
  "limit": 2,
  "offset": 0
}
```

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `items` | array(Job) | 否 | 当前页任务数组，结构见 [§3](#3-任务对象job) |
| `total` | integer | 否 | 过滤后、分页前的任务总数（实测全量为 22） |
| `limit` | integer | **是** | 本次生效的每页条数；未传 `limit` 时为 `null` |
| `offset` | integer | 否 | 本次生效的偏移量；未传 `limit` 时固定为 `0` |

分页行为（均实测）：

| 请求 | 结果 |
| --- | --- |
| 不带参数 | `items` 全量（22 条），`total` 22，`limit: null`，`offset: 0` |
| `?limit=5` | 5 条，`total` 22，`limit: 5`，`offset: 0` |
| `?limit=5&offset=20` | 2 条（最后一页不满），`total` 22，`offset: 20` |
| `?offset=5`（只给 offset） | 忽略 offset，全量 22 条，`limit: null`，`offset: 0` |
| `?limit=2&offset=999` | `items: []`，`total` 仍为 22，`offset: 999` |
| `?status=SUCCEEDED` | `total` 9，返回项状态全为 `SUCCEEDED` |
| `?q=QB` | `total` 3（`qb3`、`qb2`、`qb`），大小写不敏感 |

**常见错误**

| 状态码 | 场景 | 响应 |
| --- | --- | --- |
| 401 | 鉴权开启且 Token 缺失/错误 | `{"detail":"Bearer token required"}` |
| 422 | `limit` 不在 1–200、`offset` < 0、`status` 非枚举、`q` 超 100 字符、`limit`/`offset` 非整数 | 校验错误数组 |
| 502 / 503 | Kubernetes API 异常或不可用 | `{"detail":"..."}` |

`limit` / `status` 越界示例（实测）：

```json
{"detail":[{"type":"less_than_equal","loc":["query","limit"],"msg":"Input should be less than or equal to 200","input":"201","ctx":{"le":200}}]}
```

```json
{"detail":[{"type":"enum","loc":["query","status"],"msg":"Input should be 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED' or 'CANCELLED'","input":"NOPE","ctx":{"expected":"'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED' or 'CANCELLED'"}}]}
```

---

### 5.2 POST /jobs

**用途**：提交一个单 Master 的 Kubeflow PyTorchJob，交由 Kueue 排队准入。

**查询参数**：无。

**请求体**（`SubmitRequest`，`extra=forbid`）：

| 字段 | 类型 | 必填 | 默认值 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `name` | string | **是** | — | 1–50 字符，`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`；同名任务已存在返回 409 |
| `image` | string \| null | 否 | 平台 `default_image` | 非空白、不含 NUL；省略或 `null` 均表示用默认镜像 |
| `gpus` | integer | **是** | — | `> 0`，且必须属于 `allowed_gpus`（当前仅 `1`） |
| `queue` | string | 否 | `"default"` | 必须等于服务端配置队列，否则 422 |
| `priority` | string | 否 | `"normal"` | 枚举 `low` / `normal` / `high` |
| `command` | string \| null | 否 | 平台 `default_command` | 非空白、不含 NUL；UTF-8 编码后不超过 65536 字节 |
| `env` | object(string→string) | 否 | `{}` | 键需匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，值不含 NUL；全部键值 UTF-8 合计不超过 65536 字节 |
| `max_retries` | integer | 否 | `0` | 枚举 `0` / `1` / `2`，映射为 PyTorchJob `spec.backoffLimit`；`0` 时容器 `restartPolicy: Never`，否则 `OnFailure` |
| `termination_grace_period_seconds` | integer | 否 | `30` | 1–600，取消后容器获得的退出时间 |

> OpenAPI 中 `SubmitRequest.name` 只声明了 `minLength` / `maxLength`，上述正则由 Pydantic 校验器强制，因此 422 的 `type` 为 `value_error` 而非 `string_pattern_mismatch`。

提交成功后平台自动注入：`CHECKPOINT_DIR=/checkpoints/<name>`、只读数据集挂载 `/datasets`、可写检查点挂载 `/checkpoints`。

**响应 201**（示意：字段与提交后立即返回的状态一致，本次校验**未实际提交**）：

```json
{
  "name": "train-001",
  "status": "QUEUED",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 100",
  "env": {"WANDB_MODE": "offline"},
  "max_retries": 1,
  "termination_grace_period_seconds": 30,
  "created_at": "2026-11-09T09:00:00Z",
  "started_at": null,
  "finished_at": null,
  "duration_seconds": null,
  "workload_status": "Pending",
  "pod_status": null,
  "message": "Waiting for Kueue admission",
  "checkpoint_dir": "/checkpoints/train-001"
}
```

**常见错误**

| 状态码 | 场景 | `detail` 示例 |
| --- | --- | --- |
| 401 | 鉴权开启且 Token 缺失/错误 | `Bearer token required` |
| 409 | 同名任务已存在 | `job train-001 already exists` |
| 422 | `queue` 非服务端配置队列 | `queue must be default` |
| 422 | `gpus` 不在允许集合 | `gpus must be one of: 1` |
| 422 | `command` / `env` 超过大小上限 | `command must not exceed 65536 bytes` |
| 422 | 字段缺失、类型错误、枚举越界、多余字段、名称非法 | 校验错误数组（见 [§1.3](#13-错误格式)） |
| 503 / 502 | Kubernetes 不可用 / API 异常 | `Kubernetes API unavailable: ...` |

---

### 5.3 GET /jobs/{name}

**用途**：查询单个任务详情；Kubernetes 对象已删除但存在取消历史时，返回历史记录合成的对象。

**路径参数**

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `name` | string | 是 | 1–50 字符，`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$` |

**查询参数**：无。

**响应 200**（实测）：

```json
{
  "name": "qb",
  "status": "SUCCEEDED",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 5 --sleep 0",
  "env": {},
  "max_retries": 0,
  "termination_grace_period_seconds": 30,
  "created_at": "2026-11-09T08:18:55Z",
  "started_at": "2026-11-09T08:18:56Z",
  "finished_at": "2026-11-09T08:19:31Z",
  "duration_seconds": 35,
  "workload_status": "Finished",
  "pod_status": "Succeeded",
  "message": "PyTorchJob qb is successfully completed.",
  "checkpoint_dir": "/checkpoints/qb"
}
```

字段含义见 [§3](#3-任务对象job)。

**常见错误**

| 状态码 | 场景 | 响应 |
| --- | --- | --- |
| 404 | 任务不存在且无取消历史 | `{"detail":"job nope-does-not-exist not found"}` |
| 422 | 名称不符合规范 | 校验错误数组（`loc: ["path","name"]`） |
| 401 | 鉴权开启且 Token 缺失/错误 | `{"detail":"Bearer token required"}` |

---

### 5.4 DELETE /jobs/{name}

**用途**：取消任务——删除 PyTorchJob（级联删除 Pod）并写入取消历史。**不是**物理清除历史：删除后仍可通过 `GET /jobs/{name}` 与 `GET /jobs` 查到该任务。

**路径参数**：同 [§5.3](#53-get-jobsname)。

**查询参数 / 请求体**：无。优雅退出时长取自任务自身的 `termination_grace_period_seconds`。

**行为**

| 取消前状态 | 取消后 `status` | `message` |
| --- | --- | --- |
| `QUEUED` / `RUNNING` | `CANCELLED` | `Cancelled by user` |
| `SUCCEEDED` / `FAILED` | 保持原状态 | `Deleted after the job finished with <原状态>` |

重复调用是幂等的：第二次调用时任务对象已不存在，但命中取消历史，返回同样的历史对象（200），不会 404。

**响应 200**（实测：对已取消任务 `ga` 的 `GET`，与 DELETE 使用同一序列化逻辑）：

```json
{
  "name": "ga",
  "status": "CANCELLED",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 60 --sleep 0.5",
  "env": {},
  "max_retries": 0,
  "termination_grace_period_seconds": 5,
  "created_at": "2026-11-09T08:45:11Z",
  "started_at": null,
  "finished_at": "2026-11-09T08:45:12Z",
  "duration_seconds": null,
  "workload_status": "Cancelled",
  "pod_status": null,
  "message": "Cancelled by user",
  "checkpoint_dir": "/checkpoints/ga"
}
```

**常见错误**

| 状态码 | 场景 |
| --- | --- |
| 404 | 任务不存在且无取消历史 |
| 422 | 名称不符合规范 |
| 401 | 鉴权开启且 Token 缺失/错误 |
| 503 / 502 | 删除工作负载时 Kubernetes 异常 |

---

### 5.5 POST /jobs/{name}/clone

**用途**：以已有任务（包括已取消的历史任务）的完整配置为基础，提交一个仅改名的新任务；镜像、GPU、命令、环境变量、重试与优雅退出时长全部继承。

**路径参数**

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `name` | string | 是 | 源任务名，规则同 [§5.3](#53-get-jobsname) |

**请求体**（`CloneRequest`，`extra=forbid`）

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `name` | string | **是** | 新任务名，1–50 字符，规则同任务名规范 |

**响应 201**：新任务对象，结构与 [§3](#3-任务对象job) 一致，初始 `status` 为 `QUEUED`。

```json
{
  "name": "train-002",
  "status": "QUEUED",
  "image": "vla-smoke:latest",
  "gpus": 1,
  "queue": "default",
  "priority": "normal",
  "command": "python /app/train.py --steps 100",
  "env": {"WANDB_MODE": "offline"},
  "max_retries": 1,
  "termination_grace_period_seconds": 30,
  "created_at": "2026-11-09T09:05:00Z",
  "started_at": null,
  "finished_at": null,
  "duration_seconds": null,
  "workload_status": "Pending",
  "pod_status": null,
  "message": "Waiting for Kueue admission",
  "checkpoint_dir": "/checkpoints/train-002"
}
```

> 该示例为示意，本次校验**未实际调用** clone。

**常见错误**

| 状态码 | 场景 | `detail` 示例 |
| --- | --- | --- |
| 404 | 源任务不存在且无历史 | `job old-name not found` |
| 409 | 新任务名已被占用 | `job train-002 already exists` |
| 422 | 新名称非法 / 继承的配置在新环境下不再合法（如队列被改） | `queue must be default` 或校验错误数组 |
| 401 | 鉴权开启且 Token 缺失/错误 | `Bearer token required` |

---

### 5.6 GET /jobs/{name}/logs

**用途**：读取任务最新 Pod 中 `pytorch` 容器的日志（仅 tail，不支持 follow / since / 时间戳）。

**路径参数**：同 [§5.3](#53-get-jobsname)。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `tail_lines` | integer | 否 | 服务端 `PLATFORM_LOG_TAIL_DEFAULT`（当前 500） | 1–10000 | 返回末尾 N 行 |

**响应 200**（实测）：

```json
{
  "name": "qb",
  "pod_name": "qb-master-0",
  "logs": "step=2/5 loss=0.003739 world_size=1\nstep=3/5 loss=0.002809 world_size=1\nstep=4/5 loss=0.002110 world_size=1\nstep=5/5 loss=0.001585 world_size=1\nSMOKE_COMPLETE steps=5 checkpoint=/checkpoints/qb/smoke.pt\n"
}
```

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 任务名 |
| `pod_name` | string | **是** | 被读取的 Pod 名；无 Pod（尚未调度 / 已取消）时为 `null` |
| `logs` | string | 否 | 日志文本，无 Pod 或容器日志尚不可读时为空串 `""` |

行为细节：

- 多个 Pod 时取创建时间最新的一个。
- 容器尚未启动、Pod 已删除等 400/404 场景不会报错，返回 `logs: ""`。
- `QUEUED` 任务通常没有 Pod，返回 `pod_name: null`、`logs: ""`。

**常见错误**

| 状态码 | 场景 |
| --- | --- |
| 404 | 任务不存在且无取消历史 |
| 422 | 名称非法，或 `tail_lines` 不在 1–10000 |
| 401 | 鉴权开启且 Token 缺失/错误 |

`tail_lines` 越界响应示例（实测）：

```json
{"detail":[{"type":"greater_than_equal","loc":["query","tail_lines"],"msg":"Input should be greater than or equal to 1","input":"0","ctx":{"ge":1}}]}
```

---

### 5.7 GET /jobs/{name}/events

**用途**：读取与该任务及其 Pod 相关的 Kubernetes 事件，按 `last_seen` 倒序，用于排查调度失败、镜像拉取失败、容器退出等原因。

**路径参数**：同 [§5.3](#53-get-jobsname)。

**查询参数**：无（无分页、无类型过滤）。

**响应 200**（实测，截取前两条）：

```json
{
  "name": "qb",
  "items": [
    {
      "type": "Normal",
      "reason": "ExitedWithCode",
      "message": "Pod: vla-training.qb-master-0 exited with code 0",
      "count": 2,
      "object_kind": "PyTorchJob",
      "object_name": "qb",
      "first_seen": "2026-11-09T08:19:30Z",
      "last_seen": "2026-11-09T08:19:31Z"
    },
    {
      "type": "Normal",
      "reason": "PyTorchJobSucceeded",
      "message": "PyTorchJob qb is successfully completed.",
      "count": 1,
      "object_kind": "PyTorchJob",
      "object_name": "qb",
      "first_seen": "2026-11-09T08:19:31Z",
      "last_seen": "2026-11-09T08:19:31Z"
    }
  ]
}
```

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 任务名 |
| `items` | array(object) | 否 | 事件数组，可能为空 |
| `items[].type` | string | 是 | `Normal` / `Warning` |
| `items[].reason` | string | 是 | 事件原因，如 `Scheduled`、`Pulled`、`ExitedWithCode` |
| `items[].message` | string | 是 | 事件正文 |
| `items[].count` | integer | 否 | 发生次数，缺失时按 1 返回 |
| `items[].object_kind` | string | 是 | 关联对象类型：`PyTorchJob` 或 `Pod` |
| `items[].object_name` | string | 是 | 关联对象名 |
| `items[].first_seen` | string | 是 | 首次发生时间，K8s 未提供时为 `null` |
| `items[].last_seen` | string | 是 | 最近发生时间，用于排序 |

Kubernetes 对象已删除的历史任务返回 `{"name": "<name>", "items": []}`，不会 404。

**常见错误**

| 状态码 | 场景 |
| --- | --- |
| 404 | 任务不存在且无取消历史 |
| 422 | 名称不符合规范 |
| 401 | 鉴权开启且 Token 缺失/错误 |
| 502 / 503 | Kubernetes API 异常 |

---

## 6. 队列与集群

### 6.1 GET /queues

**用途**：返回当前 LocalQueue 的配额、已用量、Kueue 工作负载计数，以及队列内任务摘要。

**查询参数**：无。

**响应 200**（实测）：

```json
{
  "items": [
    {
      "name": "default",
      "cluster_queue": "vla-cluster-queue",
      "gpu_quota": 1,
      "gpu_used": 0,
      "pending": 0,
      "admitted": 0,
      "items": [
        {"name": "qb", "priority": "normal", "status": "SUCCEEDED", "created_at": "2026-11-09T08:18:55Z"},
        {"name": "smoke001", "priority": "normal", "status": "SUCCEEDED", "created_at": "2026-11-09T08:18:39Z"},
        {"name": "w", "priority": "normal", "status": "SUCCEEDED", "created_at": "2026-11-09T07:49:44Z"},
        {"name": "verify4090-1794207666-2216767-a", "priority": "normal", "status": "SUCCEEDED", "created_at": "2026-11-09T07:01:06Z"}
      ]
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `items` | array(object) | 固定只有一个元素（当前只支持单 LocalQueue） |
| `items[].name` | string | LocalQueue 名，等于 `default_queue` |
| `items[].cluster_queue` | string | 对应 ClusterQueue 名 |
| `items[].gpu_quota` | integer | ClusterQueue 对 `nvidia.com/gpu` 的 nominalQuota 合计 |
| `items[].gpu_used` | integer | 已用 GPU；优先取 LocalQueue `status.flavorsUsage`，缺失时按已准入 Workload 汇总 |
| `items[].pending` | integer | 待准入 Workload 数；优先取 `status.pendingWorkloads`，缺失时按 `Pending` 条件统计 |
| `items[].admitted` | integer | 已准入 Workload 数，来源同上 |
| `items[].items` | array(object) | 队列内任务摘要：`name`、`priority`、`status`、`created_at` |
| `items[].items[].status` | string | 与任务对象的 `status` 相同 |

注意：

- 队列内任务仅包含 `queue == default` 的**活跃** PyTorchJob；已取消、对象已删除的历史任务不会出现在 `items[].items` 中（`GET /jobs` 才会包含）。
- `pending` / `admitted` 的回退统计基于命名空间内全部 Workload，多队列部署时可能偏大（**待确认**，当前单队列部署无影响）。

**常见错误**

| 状态码 | 场景 | `detail` 示例 |
| --- | --- | --- |
| 503 | LocalQueue 不存在 | `Kueue LocalQueue default not found` |
| 503 | ClusterQueue 不存在 | `Kueue ClusterQueue vla-cluster-queue not found` |
| 401 | 鉴权开启且 Token 缺失/错误 | `Bearer token required` |
| 502 / 503 | Kubernetes API 异常 | `Kubernetes API error: ...` |

### 6.2 GET /cluster/summary

**用途**：集群 GPU 总量/已分配/可用量、节点明细、`nvidia-smi` 实时指标与任务状态计数，供仪表盘使用。

**查询参数**：无。

**响应 200**（实测）：

```json
{
  "gpu_total": 1,
  "gpu_allocated": 0,
  "gpu_available": 1,
  "node_count": 1,
  "nodes": [
    {
      "name": "lenovo-thinkstation-p3-tower-gen-2",
      "ready": true,
      "gpu_total": 1,
      "gpu_allocated": 0
    }
  ],
  "gpu_metrics": {
    "available": true,
    "items": [
      {
        "index": 0,
        "name": "NVIDIA GeForce RTX 4090",
        "utilization_percent": 100,
        "memory_used_mib": 38747,
        "memory_total_mib": 49140,
        "temperature_celsius": 61
      }
    ]
  },
  "jobs": {"queued": 0, "running": 0, "succeeded": 9, "failed": 0, "cancelled": 13}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `gpu_total` | integer | 各节点 `nvidia.com/gpu` 可分配量之和 |
| `gpu_allocated` | integer | 未结束 Pod 的 GPU 请求之和 |
| `gpu_available` | integer | `max(0, gpu_total - gpu_allocated)` |
| `node_count` | integer | 节点数 |
| `nodes` | array(object) | 节点明细 |
| `nodes[].name` | string | 节点名 |
| `nodes[].ready` | boolean | `Ready` 条件是否为 True |
| `nodes[].gpu_total` | integer | 该节点 GPU 可分配量（优先 `allocatable`，回退 `capacity`） |
| `nodes[].gpu_allocated` | integer | 该节点上未结束 Pod 的 GPU 请求之和 |
| `gpu_metrics.available` | boolean | 指标服务是否可用 |
| `gpu_metrics.items` | array(object) | 每卡指标；服务不可用时为 `[]` |
| `gpu_metrics.items[].index` | integer | GPU 序号（升序） |
| `gpu_metrics.items[].name` | string | GPU 型号，未知时为 `unknown` |
| `gpu_metrics.items[].utilization_percent` | integer | 利用率百分比 |
| `gpu_metrics.items[].memory_used_mib` | integer | 已用显存 MiB |
| `gpu_metrics.items[].memory_total_mib` | integer | 总显存 MiB |
| `gpu_metrics.items[].temperature_celsius` | integer | 温度摄氏度 |
| `jobs.queued` / `running` / `succeeded` / `failed` / `cancelled` | integer | 按任务 `status` 小写分类计数；含取消历史任务 |

注意：

- `gpu_metrics` 由 `vla-gpu-metrics` DaemonSet 提供，采集失败不会让接口报错，而是返回 `{"available": false, "items": []}`；此时其它字段仍然有效。
- `gpu_allocated` 只统计集群内 Pod，宿主机上绕过 Kubernetes 使用 GPU 的进程不计入，因此可能出现利用率高但 `gpu_allocated` 为 0（如本次实测）。

**常见错误**

| 状态码 | 场景 |
| --- | --- |
| 401 | 鉴权开启且 Token 缺失/错误 |
| 502 / 503 | Kubernetes API 异常或不可用 |

---

## 7. 任务状态机

```
                    POST /jobs
                        │
                        ▼
                 ┌─────────────┐
                 │   QUEUED    │◀── 尚未准入 / 已准入但 Pod 未就绪
                 └──────┬──────┘
                        │ Kueue 准入 + Master Pod 启动
                        ▼
                 ┌─────────────┐
                 │   RUNNING   │
                 └──┬───────┬──┘
       退出码 0 /    │       │  非 0 退出 / Pod Failed
       Succeeded     ▼       ▼
              ┌───────────┐ ┌────────┐
              │ SUCCEEDED │ │ FAILED │
              └───────────┘ └────────┘

  QUEUED / RUNNING ──DELETE /jobs/{name}──▶ CANCELLED
  SUCCEEDED / FAILED ──DELETE /jobs/{name}──▶ 仍为 SUCCEEDED / FAILED（仅清理）
```

**状态判定顺序**（服务端按此优先级计算 `status`，与 `workload_status`、`pod_status` 相互独立）：

1. PyTorchJob 存在生效的 `Succeeded` 或 `Failed` 条件 → `SUCCEEDED` / `FAILED`；
2. 否则 Pod phase 为 `Succeeded` / `Failed` → 对应终态；
3. 否则 PyTorchJob 存在 `Running` 条件，或 Pod phase 为 `Running` → `RUNNING`；
4. 其余 → `QUEUED`。

**各状态含义**

| 状态 | 是否终态 | 触发条件 | 典型 `message` |
| --- | --- | --- | --- |
| `QUEUED` | 否 | 尚未被 Kueue 准入，或已准入但 Pod 未启动 | `Waiting for Kueue admission` / `Admitted by Kueue; waiting for training pod` / `Admitted but pod is not ready: ImagePullBackOff` |
| `RUNNING` | 否 | 训练容器已启动 | `Training job is running` |
| `SUCCEEDED` | 是 | 训练正常结束 | `PyTorchJob <name> is successfully completed.` |
| `FAILED` | 是 | 容器非 0 退出且重试耗尽 | 通常为 PyTorchJob 的失败条件消息 |
| `CANCELLED` | 是 | 仅由 `DELETE /jobs/{name}` 对未终态任务产生 | `Cancelled by user` |

> `message` 是展示用文本，不保证唯一对应某个状态：例如终态条件缺少 `message` 时，会回退到 `Admitted by Kueue; waiting for training pod` 一类的分支文案。调用方应以 `status` 为准，不要解析 `message`。

**关于 `CANCELLED`**

- Kubernetes 侧没有 `CANCELLED` 状态；它是删除工作负载后由平台历史库（SQLite `cancellations` 表）补出的虚拟状态。
- 任务对象删除后，`GET /jobs/{name}`、`GET /jobs`、`/logs`、`/events` 仍可访问：详情与列表返回历史对象，日志返回空串，事件返回空数组。
- 取消后**可以复用同名**重新提交（Kubernetes 对象已不存在，不触发 409）；新任务会成为 live 对象，列表中以新状态展示，旧历史被掩盖。
- 不存在 `PAUSED`、`RESTARTING`、`QUEUED` 回流等状态；`max_retries` 的重试发生在同一个 PyTorchJob 内，对外仍表现为 `RUNNING` / `QUEUED`。
- 终态不会回退；再次 `DELETE` 一个终态任务只做清理，并在历史中保留原终态。

**三个状态字段的关系**

| 字段 | 取值来源 | 用途 |
| --- | --- | --- |
| `status` | 平台归一化结果 | 调用方判断生命周期的唯一依据 |
| `workload_status` | Kueue Workload conditions | 区分「等配额」与「等 Pod」 |
| `pod_status` | Pod phase | 排查容器级问题 |

---

## 8. 错误码汇总

| 状态码 | 含义 | `detail` 形态 | 典型触发 |
| --- | --- | --- | --- |
| 200 | 成功 | — | GET / DELETE |
| 201 | 已创建 | Job 对象 | POST /jobs、POST clone |
| 401 | 未认证 | string | 鉴权开启时 Token 缺失或错误 |
| 404 | 资源不存在 | string | 任务不存在、`/api/v1` 下未知路径 |
| 409 | 冲突 | string | 同名任务已存在 |
| 422 | 参数/请求体非法 | string 或 array | 队列/GPU 不允许、载荷过大、字段校验失败、名称非法 |
| 500 | 未捕获异常 | string | 服务端内部错误（`internal server error`） |
| 502 | 上游异常 | string | Kubernetes API 返回非 401/403/5xx 的错误 |
| 503 | 依赖不可用 | string | Kubernetes 配置/连接失败、LocalQueue/ClusterQueue 缺失 |

---

## 9. curl 快速上手

```bash
export BASE=http://127.0.0.1:30080/api/v1
# 鉴权开启时追加：export TOKEN=<your-token>，并在每条请求加 -H "Authorization: Bearer $TOKEN"
```

**1）确认服务与鉴权模式**

```bash
curl -fsS "$BASE/health"
# {"status":"ok","auth_required":false}
```

**2）查看平台默认值**

```bash
curl -fsS "$BASE/config"
```

**3）列出任务（过滤 + 分页）**

```bash
curl -fsS "$BASE/jobs"                              # 全量：limit=null, offset=0
curl -fsS "$BASE/jobs?limit=20&offset=0"            # 第 1 页
curl -fsS "$BASE/jobs?status=RUNNING"               # 只看运行中
curl -fsS "$BASE/jobs?q=qm&limit=10"                # 名称包含 qm（大小写不敏感）
```

**4）提交任务**（`image` 省略即用默认镜像；示例，未在本次校验中执行）

```bash
curl -fsS -X POST "$BASE/jobs" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "train-001",
    "gpus": 1,
    "queue": "default",
    "priority": "normal",
    "command": "python /app/train.py --steps 100",
    "env": {"WANDB_MODE": "offline"},
    "max_retries": 1,
    "termination_grace_period_seconds": 30
  }'
```

**5）轮询状态**

```bash
curl -fsS "$BASE/jobs/train-001"
# 关注 status / workload_status / message
```

**6）看日志与事件**

```bash
curl -fsS "$BASE/jobs/train-001/logs?tail_lines=100"
curl -fsS "$BASE/jobs/train-001/events"
```

**7）克隆与取消**

```bash
curl -fsS -X POST "$BASE/jobs/train-001/clone" \
  -H 'Content-Type: application/json' \
  -d '{"name": "train-002"}'

curl -fsS -X DELETE "$BASE/jobs/train-001"
```

**8）集群与队列**

```bash
curl -fsS "$BASE/queues"
curl -fsS "$BASE/cluster/summary"
```

**9）脚本中的错误处理**

```bash
# -f 让 4xx/5xx 变成非 0 退出码；错误体仍是 {"detail": ...}
if ! curl -fsS "$BASE/jobs/train-001" -o /tmp/job.json; then
  echo "request failed: $(cat /tmp/job.json 2>/dev/null)"
fi
```

---

## 10. 附录：文档来源与校验

### 10.1 OpenAPI 生成

未启动长期服务，直接在 `backend/` 的包路径下导入应用生成（任务给出的命令缺少 `PYTHONPATH`，会因为 `app` 不在搜索路径而报 `ModuleNotFoundError`，实际可用命令如下）：

```bash
cd ~/vla-train-platform
PYTHONPATH=backend PLATFORM_AUTH_REQUIRED=false PLATFORM_DB_PATH=/tmp/openapi.db \
  .venv/bin/python -c "
import json
from app.main import app
print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
" > /tmp/openapi.json
```

生成成功：9 个路径、11 个操作，`components.schemas` 含 `SubmitRequest`、`CloneRequest`、`JobStatus`、`HTTPValidationError`、`ValidationError`。

分页功能部署到 30080 后（2026-11-09 17:05 CST）重新生成并对比：线上 `/openapi.json` 的 `paths`（含 `GET /jobs` 的 `limit` / `offset` / `status` / `q` 四个查询参数）、`components.schemas` 与本地生成结果**完全一致**，说明部署版本与工作区源码同步，本文描述即为线上行为。

### 10.2 实测范围

实测时间：2026-11-09 17:05 CST（09:05 UTC），目标 `http://127.0.0.1:30080`，`auth_required=false`。

| 端点 | 实测 | 备注 |
| --- | --- | --- |
| `GET /health` | ✅ | 字段、值一致 |
| `GET /config` | ✅ | 7 个字段一致 |
| `GET /jobs` | ✅ | `items` + `total` + `limit` + `offset`；默认全量 22 条、`limit: null` |
| `GET /jobs?limit=2` | ✅ | 2 条，`total` 22，`limit` 2，`offset` 0 |
| `GET /jobs?limit=5&offset=20` | ✅ | 末页 2 条，`total` 22，`offset` 20 |
| `GET /jobs?offset=5` | ✅ | 无 `limit` 时 offset 被忽略（`offset: 0`，全量 22 条） |
| `GET /jobs?status=SUCCEEDED` | ✅ | `total` 9，返回项状态全为 `SUCCEEDED` |
| `GET /jobs?q=QB` | ✅ | `total` 3（`qb3`、`qb2`、`qb`），大小写不敏感 |
| `GET /jobs?limit=0` / `limit=201` / `status=NOPE` / `offset=-1` / `q` 超 100 字符 | ✅ | 422 校验错误体 |
| `GET /jobs/qb` | ✅ | 成功态示例 |
| `GET /jobs/ga` | ✅ | 取消历史示例 |
| `GET /jobs/qb/logs` | ✅ | `tail_lines=5` |
| `GET /jobs/qb/events` | ✅ | 事件字段与排序 |
| `GET /queues` | ✅ | 单队列、4 条任务摘要 |
| `GET /cluster/summary` | ✅ | 节点、GPU 指标、任务计数 |
| `GET /jobs/nope-does-not-exist` | ✅ | 404 错误体 |
| `GET /jobs/Bad_Name` | ✅ | 422 校验错误体 |
| `GET /jobs/qb/logs?tail_lines=0` | ✅ | 422 越界错误体 |
| `POST /jobs` | ❌ 未执行 | 会创建训练任务；请求/响应示例据源码构造 |
| `DELETE /jobs/{name}` | ❌ 未执行 | 会影响他人任务；返回结构取自真实取消历史 |
| `POST /jobs/{name}/clone` | ❌ 未执行 | 会产生新任务 |

### 10.3 未实测、依据源码的结论（待确认）

| 项 | 依据 |
| --- | --- |
| 401 的两种 `detail` 文案与 `WWW-Authenticate` 头 | `backend/app/auth.py`；当前部署鉴权关闭，无法触发 |
| 409 同名冲突文案 `job <name> already exists` | `backend/app/services/platform.py::submit` |
| 503 `Kueue LocalQueue/ClusterQueue ... not found` | `platform.py::queues`；当前环境队列正常 |
| 502 语义（K8s 非 401/403/5xx 错误） | `backend/app/services/kubernetes.py::_call` |

### 10.4 已知文档边界

- `GET /jobs` 默认返回全部任务（不传 `limit` 即不分页），任务量大时建议显式传 `limit`/`offset`；`offset` 只在同时传 `limit` 时生效。
- 单队列限制：`queue` 必须等于服务端 `PLATFORM_QUEUE`，多队列部署需先改服务端。
- `GET /queues` 的 `pending` / `admitted` 回退值基于命名空间全部 Workload，多队列时含义会偏移（**待确认**）。
- 日志接口仅支持 tail，不支持 `follow`、时间范围、指定容器。