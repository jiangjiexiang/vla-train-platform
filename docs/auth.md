# 鉴权、用户与配额

本文描述 v0.9.0 引入的三种鉴权模式、LDAP 接入口径和按用户 GPU/任务数配额。
字段与端点的权威定义见 `CONTRACTS.md` 的 v0.9 段，HTTP 细节见
[`api.md`](api.md) 第 4.3–4.7 节。

> 适用版本：0.9.0。v0.8 及以前的 `PLATFORM_AUTH_REQUIRED` 行为完整保留，默认
> 行为不变（`token` 模式 + 静态令牌）。

## 0. 一张表看懂三种模式

| 模式 | 触发方式 | 登录方式 | 当前用户 | 角色 | `/health` 的 `auth_required` |
|---|---|---|---|---|---|
| `off` | `PLATFORM_AUTH_MODE=off`，或 `PLATFORM_AUTH_REQUIRED=false` | 无 | `anonymous` | `admin` | `false` |
| `token` | 默认；或 `PLATFORM_AUTH_MODE=token` | 自带静态令牌 | `token` | `admin` | `true` |
| `ldap` | `PLATFORM_AUTH_MODE=ldap` | 用户名 + 密码换平台会话令牌 | LDAP 的 `uid` | 按组映射 | `true` |

`PLATFORM_AUTH_REQUIRED=false` 等价于 `PLATFORM_AUTH_MODE=off`；两者同时出现时以
`off` 为准（关闭优先，不会因为误设 `PLATFORM_AUTH_MODE` 而把登录重新打开）。

## 0.1 未登录时前端怎么判断模式

`GET /api/v1/health` 是唯一的免鉴权端点，返回：

```json
{"status": "ok", "auth_required": false, "auth_mode": "off"}
```

前端据此决定界面：`off` 直接以匿名 admin 进入；`token` 显示访问令牌输入框；
`ldap` 显示用户名/密码表单。`auth_mode` 是 v0.9 新增键，旧客户端忽略它即可。

## 1. 角色与可见范围

| 角色 | 能做什么 |
|---|---|
| `admin` | 提交、取消、克隆；查看**所有**用户的任务；`GET /users`；`PUT /users/{username}/quota` |
| `user` | 提交、克隆；查看与取消**自己**的任务；`GET /jobs` 只返回自己提交的任务 |

- `GET /jobs` 的过滤发生在服务端，`user` 角色即使改查询参数也只返回自己提交的任务
  （`user` 读取他人任务详情、日志、事件或取消他人任务，一律返回 404 `job <name> not found`，
  不返回 403，避免泄露任务是否存在）。
- admin 同样受配额约束，额度不够时只能先调高自己的配额，不能绕过。
- 任务响应新增 `owner` 字段。v0.8 之前提交的老任务和历史记录没有 owner，读取时回填为
  空串 `""`，不做数据迁移；这些任务只有 admin 能看到。

### 3.1 登录后的会话复查

每次带会话令牌的请求，服务端都会用只读服务账号复查一次 LDAP：账号被删除或移出
admin 组会立即生效（401 并撤销会话）。LDAP 暂时不可达时**不会踢掉已有会话**，
按登录时解析出的角色继续放行，避免目录抖动导致全员掉线。

## 2. 三种模式怎么选

| 场景 | 建议 |
|---|---|
| 可信内网做验收、演示 | `off`，最快，无任何凭据 |
| 内网长期使用、只有一个团队/一台脚本机 | `token`，一个共享令牌，与 v0.8 完全一致 |
| 需要区分「谁提交的」「谁能提交几次」 | `ldap`，公司账号直接登录，并按用户配额 |

`off` **只适合可信内网和短期场景**：它等于所有人都是 admin，没有审计，无法区分提交者。
风险清单见 [`operations.md` 第 6.1 节](operations.md#61-当前状态和风险)。

## 3. 配置项

全部通过环境变量注入，由 `backend/app/config.py` 的 `Settings.from_env()` 读取。

| 变量 | 默认 | 适用模式 | 说明 |
|---|---|---|---|
| `PLATFORM_AUTH_MODE` | `token` | 全部 | `off` / `token` / `ldap` |
| `PLATFORM_AUTH_REQUIRED` | `true` | 兼容 | 为 `false` 时强制 `off` |
| `PLATFORM_API_TOKEN` | 空 | `token` | 静态共享令牌；`token` 模式下必须非空 |
| `PLATFORM_SESSION_TTL_HOURS` | `12` | `ldap` | 会话令牌有效期（小时） |
| `PLATFORM_LDAP_URL` | 空 | `ldap` | LDAP/AD 地址，可逗号分隔多个做故障切换 |
| `PLATFORM_LDAP_BASE_DN` | 空 | `ldap` | 搜索/绑定的基准 DN，如 `dc=corp,dc=com` |
| `PLATFORM_LDAP_USER_TEMPLATE` | 空 | `ldap` | 用户 DN 模板，如 `uid={username},ou=people,dc=corp,dc=com` |
| `PLATFORM_LDAP_GROUP_DN_ADMIN` | 空 | `ldap` | admin 组 DN；成员为 `admin`，其余为 `user` |
| `PLATFORM_LDAP_BIND_DN` | 空 | `ldap` | 可选：复查会话用的只读服务账号 DN；不配则匿名查询 |
| `PLATFORM_LDAP_BIND_PASSWORD` | 空 | `ldap` | 上者的密码，**只从 Secret 注入**（`vla-web-ldap`） |
| `PLATFORM_LDAP_USER_WHITELIST` | 空 | `ldap` | 可选：逗号分隔的用户白名单，非空时只有名单内用户可登录 |
| `PLATFORM_DEFAULT_MAX_GPUS` | 空（不限） | 全部 | 单用户 GPU 上限的默认值 |
| `PLATFORM_DEFAULT_MAX_ACTIVE_JOBS` | 空（不限） | 全部 | 单用户活跃任务数上限的默认值 |
| `PLATFORM_MAX_NODES` | `1` | 全部 | 单任务最大节点数；`1` 表示关闭多机（见 [`multi-node.md`](multi-node.md)） |

启动失败（进程退出、Pod `CrashLoopBackOff`）的两种配置：

- `token` 模式下 `PLATFORM_API_TOKEN` 为空：沿用 v0.8 的
  `PLATFORM_API_TOKEN is required when PLATFORM_AUTH_REQUIRED is true`。
- `PLATFORM_AUTH_MODE` 取值不是 `off` / `token` / `ldap`：直接拒绝启动，不要靠猜测兜底。

> `ldap` 模式下 `PLATFORM_API_TOKEN` 可留空；会话令牌由登录接口签发，不再需要静态令牌。

## 4. LDAP / AD 接入

### 4.1 配置示例

OpenLDAP（`uid` 在 `ou=people` 下，管理员组用 `member` 成员属性）：

```yaml
env:
  - name: PLATFORM_AUTH_MODE
    value: ldap
  - name: PLATFORM_SESSION_TTL_HOURS
    value: "12"
  - name: PLATFORM_LDAP_URL
    value: "ldap://ldap.corp.example.com:389,ldap://ldap2.corp.example.com:389"
  - name: PLATFORM_LDAP_BASE_DN
    value: dc=corp,dc=example,dc=com
  - name: PLATFORM_LDAP_USER_TEMPLATE
    value: uid={username},ou=people,dc=corp,dc=example,dc=com
  - name: PLATFORM_LDAP_GROUP_DN_ADMIN
    value: cn=vla-admin,ou=groups,dc=corp,dc=example,dc=com
```

AD（`sAMAccountName` 登录，用 `UPN` 模板可直接带域后缀）：

```yaml
env:
  - name: PLATFORM_AUTH_MODE
    value: ldap
  - name: PLATFORM_LDAP_URL
    value: ldaps://ad.corp.example.com:636
  - name: PLATFORM_LDAP_BASE_DN
    value: dc=corp,dc=example,dc=com
  - name: PLATFORM_LDAP_USER_TEMPLATE
    value: "'{username}@corp.example.com'"
  - name: PLATFORM_LDAP_GROUP_DN_ADMIN
    value: cn=VLA-Admins,ou=Groups,dc=corp,dc=example,dc=com
```

`PLATFORM_LDAP_URL` 写多个地址时按顺序尝试，前一个连接失败再试下一个；全部失败才是
503。用 `ldaps://` 或 `ldap://...:389` + StartTLS 取决于公司目录服务要求，平台不做明文
降级。

### 4.2 用户信息从哪来

| 字段 | 来源 |
|---|---|
| `username` | LDAP 条目的 `uid`（AD 上是 `sAMAccountName`） |
| `role` | 属于 `PLATFORM_LDAP_GROUP_DN_ADMIN` → `admin`，否则 `user` |
| `source` | 固定 `ldap` |

### 4.3 登录流程

```text
浏览器 ──POST /auth/login {username, password}──▶ Web
                                                   │ 1. 按 USER_TEMPLATE 拼 DN
                                                   │ 2. 用该 DN + 密码绑定
                                                   │ 3. 检查组成员
                                                   │ 4. 签发会话令牌（SHA-256 可存哈希）
        ◀──200 {token, expires_at, user}───────────┘
浏览器 ──Authorization: Bearer <会话令牌>──▶ 其余 /api/v1/*
```

- 会话令牌是**不透明字符串**，LDAP 密码不下发、不落库、不写日志。
- 服务端只保存令牌的 SHA-256 哈希；数据库泄露也不等于令牌泄露。
- 有效期 `PLATFORM_SESSION_TTL_HOURS`，过期后所有请求 401，需要重新登录。
- 登出（`POST /auth/logout`）使当前令牌立即失效，不等过期。
- 令牌存在 SQLite（与历史库同一个文件），**Web 重启后仍有效**。

## 5. 令牌与请求头口径

```
Authorization: Bearer <令牌>
```

服务端解析顺序：

1. 先查会话表（LDAP 登录签发的令牌）；
2. 未命中，且当前是 `token` 模式时，再与 `PLATFORM_API_TOKEN` 做常数时间比较
   （`hmac.compare_digest`）；
3. 都未命中 → 401。

因此：

- `token` 模式下，浏览器在 `/login` 填写的「访问令牌」就是 `PLATFORM_API_TOKEN`，CLI 的
  `vla-train config --token` 也用它，行为与 v0.8 完全一致。
- `ldap` 模式下，浏览器走用户名/密码表单，后续请求带的是会话令牌；CLI 目前仍按静态
  令牌方式发送 `Authorization: Bearer`，把**会话令牌**写进 `--token` 即可（相当于手工
  做了一次登录），令牌过期后需要重新获取。
- `off` 模式下，请求带 `Authorization` 也会被忽略，不会报错。

既有 401 的 JSON 形状保持不变：`{"detail": "Bearer token required"}`（未带令牌或 scheme
不是 `bearer`）、`{"detail": "Invalid bearer token"}`（令牌不匹配），响应头
`WWW-Authenticate: Bearer`。登录接口的 401 是另一套文案，见下节。

## 6. 按用户配额

### 6.1 两个上限

| 配额 | 含义 | 默认来自 |
|---|---|---|
| `max_gpus` | 该用户所有非终态任务（`QUEUED` + `RUNNING`）的 GPU 之和上限 | `PLATFORM_DEFAULT_MAX_GPUS` |
| `max_active_jobs` | 该用户非终态任务个数上限 | `PLATFORM_DEFAULT_MAX_ACTIVE_JOBS` |

- 两个变量留空 = 不限；填正整数即成为所有用户的默认配额。
- 单用户覆盖存 SQLite 表 `user_quotas(username, max_gpus, max_active_jobs, updated_at)`，
  通过 `PUT /users/{username}/quota` 修改；把某一项设为 `null` 表示**对该用户不限**
  （这是覆盖值，不会回落到全局默认）。
- 计数口径：K3s 中的实时任务 ∪ 历史中未取消的记录；取消后额度**立即释放**。
- 提交被拒是 422，`detail` 是固定格式的字符串，可直接展示给用户：

```json
{"detail": "GPU quota exceeded: 4 used of 4 allowed"}
{"detail": "Active job quota exceeded: 2 of 2 allowed"}
```

前者按「已用 + 本次请求」判断：申请 1 卡、已用 4 卡、上限 4 卡时同样拒绝。

### 6.2 典型分配

4090 单卡机器（`nominalQuota: 1`）上，配额只决定「谁能排队」，不改变 Kueue 放行：

| 用户 | `max_gpus` | `max_active_jobs` | 效果 |
|---|---|---|---|
| 算法 A | 1 | 2 | 最多 1 卡在跑或排队 + 1 个排队任务 |
| 算法 B | 1 | 1 | 只能提 1 个任务，避免一个人把队列占满 |
| 管理员 | `null` | `null` | 不限，便于救火 |

A100（8 卡）上常见做法是 `max_gpus: 8`，把排队压力交给 Kueue，配额只用于防滥用。

### 6.3 查看与调整

```bash
export BASE=http://127.0.0.1:30080/api/v1
export TOKEN=<admin 令牌或会话令牌>

# 当前用户 + 生效配额
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/auth/me"

# 所有用户：角色、最近活跃、活跃任务数、GPU 占用、配额
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/users"

# 把 alice 限制为 4 卡 / 2 个活跃任务
curl -fsS -X PUT "$BASE/users/alice/quota" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_gpus": 4, "max_active_jobs": 2}'

# 取消 alice 的单用户覆盖（回到全局默认）
curl -fsS -X PUT "$BASE/users/alice/quota" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_gpus": null, "max_active_jobs": null}'
```

网页在 `/users` 页面（仅 admin 可见）做同样的操作，任务页会显示当前用户剩余额度。

## 7. 故障排查：401 还是 503

两条原则先记住：

| 现象 | 含义 | 该查什么 |
|---|---|---|
| 登录返回 **401** | 用户名/密码确实不对 | 让用户核对密码；确认账号未被锁定 |
| 登录返回 **503** | 平台**连不上** LDAP | 网络、DNS、证书、DN 模板；不要当成密码错误反复重试 |

401 的 `detail` 统一是 `Invalid username or password`，**不区分**「用户不存在」和「密码错」
（避免枚举账号）。因此看到一个 401 时，不要再去猜是哪种：两种情况在外部完全等价。

### 7.1 503：连不上目录服务

判断依据：

```bash
# 1. Web 日志里 LDAP 连接错误
kubectl -n vla-platform logs deploy/vla-web --tail=200 | grep -i -E 'ldap|503'
# 2. 从 Web 容器内测网络可达性（不要在宿主机上测完就下结论）
kubectl -n vla-platform exec deploy/vla-web -- \
  python3 -c "import socket;s=socket.create_connection(('ldap.corp.example.com',389),5);print('tcp ok');s.close()"
# 3. 当前生效的 LDAP 配置
kubectl -n vla-platform get deploy vla-web -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep LDAP
curl -fsS "$BASE/health"
```

常见原因：

| 原因 | 判断方法 | 处理 |
|---|---|---|
| URL 写错 / DNS 解析失败 | 上面第 2 步报 `Name or service not known` | 修正 `PLATFORM_LDAP_URL` |
| 防火墙或安全组阻断 389/636 | TCP 连接超时 | 放通 Web Pod 所在节点到 LDAP 的出站 |
| 使用 `ldaps://` 但证书不受信 | 日志里出现证书校验错误 | 把公司 CA 装进镜像或节点信任链，不要关闭校验 |
| 多个 URL 全部不通 | 日志里多个地址依次失败 | 至少恢复一个可达地址 |
| DN 模板拼错（如多余空格） | 连接成功但绑定持续报“无效 DN”类错误 | 核对 `PLATFORM_LDAP_USER_TEMPLATE`，`{username}` 只能用一次 |

连接层失败**必须**是 503。如果看到 401 而日志里其实是连接超时，属于实现缺陷，按 bug
上报，不要用「密码可能错了」敷衍。

### 7.2 401：登录被判无效

| 场景 | 判断 |
|---|---|
| 密码确实错误 | 用其他工具（如 `ldapwhoami -D <DN> -W`）验证同一 DN |
| 用户不在 `ou=people` 下 | 用管理员账号 `search` 一下该 uid 的真实 DN，与模板对比 |
| AD 账号需要域前缀 | 把模板改成 `{username}@corp.example.com` 或 `CORP\\{username}` |
| 账号被锁定/过期 | 由目录服务返回的实际原因决定，平台侧只表现为 401 |

### 7.3 登录成功但马上 401

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录后立刻操作就 401 | 令牌已过 `PLATFORM_SESSION_TTL_HOURS` | 重新登录；按需调大 TTL |
| 换了 Web 实例后老令牌失效 | 会话表不在同一 SQLite 文件 | 确认 `PLATFORM_DB_PATH` 指向持久卷，多副本场景见 [`high-availability.md`](high-availability.md) |
| 手工粘贴令牌多了空格/换行 | 头里带了不可见字符 | 用 `$(cat token.txt)` 或环境变量，不要 `echo` 后复制 |
| 网页一直提示未登录 | 前端没有拿到令牌（清过缓存/换了浏览器） | 重新登录，检查浏览器是否禁用 localStorage |

### 7.4 登录正常但接口 403 / 看不到任务

- `GET /users`、`PUT /users/{name}/quota` 只有 admin 可调用（状态码以 `api.md` §4.5 为准）。
- 看不到别人的任务不是 403：`user` 角色下 `GET /jobs` 只返回自己的任务。
- 确认角色映射：同名用户在 `PLATFORM_LDAP_GROUP_DN_ADMIN` 里吗？组成员检查依赖
  `member`/`memberOf` 语义，AD 上通常需要 `memberOf` 已由目录同步生成。

## 8. 安全建议

- 不要把 `PLATFORM_API_TOKEN`、会话令牌写进仓库、脚本、命令行历史或聊天记录；尽量用
  Secret + 环境变量注入。
- `off` 模式只用于可信内网。切到 `token` / `ldap` 之前，先在交换机 ACL 或安全组上限制
  30080 的来源。
- 定期轮换 `PLATFORM_API_TOKEN`；轮换后旧客户端全部 401，属预期。
- LDAP 绑定只用用户自己的凭据，平台不保存服务账号密码；不要为了省事引入一个高权限
  服务账号做搜索绑定。
- 会话令牌存的是哈希，但 SQLite 文件本身仍应限制属主（当前容器以 uid `10001` 运行）。