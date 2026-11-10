# 登录与账号

平台支持四种认证模式，按部署场景选一种。

| 模式 | 登录方式 | 适用场景 |
|---|---|---|
| `off` | 不需要登录 | 单机自用、内网隔离 |
| `local` | **账号密码** | 多人共用一台机器 |
| `ldap` | 域账号 | 已有企业目录服务 |
| `token` | 粘贴令牌 | 机器调用、CI |

当前部署用的是 `local`。

## 本地账号怎么用

打开 `http://172.16.122.37:30080`，在登录页输入：

| 账号 | 角色 | 能做什么 |
|---|---|---|
| `admin` | admin | 全部操作：提交、取消、改配额、清理历史、看审计 |
| `viewer` | user | 只能看和操作自己的任务 |

**密码不写在文档里**：这个仓库可能被多人访问，把在用的密码写进版本历史，
等于把它公开。初始密码请向部署者索取，首次登录后立刻改掉（见下面「增改账号」）。

账号本身由 Secret `vla-web-auth` 的 `PLATFORM_LOCAL_USERS` 提供，
格式是 `用户名:角色:密码哈希`，存的是 PBKDF2 哈希而不是明文。

## 增改账号

账号配置在 Secret 的 `PLATFORM_LOCAL_USERS` 里，格式是
`用户名:角色:密码哈希`，逗号分隔。**不存明文密码**——
环境变量会被 `kubectl describe deploy` 看到，也会出现在
pod 的 `/proc/<pid>/environ` 里，存明文等于把密码写在墙上。

### 生成密码哈希

在仓库目录下执行：

```bash
cd /home/jjx/vla-train-platform
source .venv/bin/activate
python3 -c "
import sys; sys.path.insert(0, 'backend')
from app.local_users import hash_password
print(hash_password('你的新密码'))
"
```

输出形如 `pbkdf2_sha256$260000$<salt>$<hash>`。

### 更新配置

```bash
ADMIN_TOKEN=<你的管理员令牌>

kubectl -n vla-platform create secret generic vla-web-auth \
  --from-literal=PLATFORM_API_TOKEN="$ADMIN_TOKEN" \
  --from-literal=PLATFORM_LOCAL_USERS="admin:admin:<哈希1>,viewer:user:<哈希2>" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n vla-platform rollout restart deploy/vla-web
```

### 配置写错会怎样

格式不对、用户名重复、角色不是 `admin`/`user`、哈希格式不对——
**服务启动直接失败**，不会静默跳过。

这是刻意的：静默跳过的后果是运维以为账号建好了，实际谁都登不进去，
而且要等到有人报告「登不上」才会发现。

## 令牌方式

`local` 模式下仍然可以用令牌直连 API（内部会走 token 校验分支）：

```bash
# 用账号密码换令牌
curl -X POST http://172.16.122.37:30080/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<你的密码>"}'

# 返回 {"token": "...", "expires_at": "...", "user": {...}}
# 之后带上：
curl -H "Authorization: Bearer <token>" http://172.16.122.37:30080/api/v1/jobs
```

## 安全说明

- 密码用 PBKDF2-SHA256 哈希，26 万次迭代，单次校验约 38ms
- 用户名不存在时也做一次哈希运算，避免响应时间泄露用户名是否存在
- 哈希比较用 `hmac.compare_digest`，常量时间
- 登录失败一律返回「用户名或密码错误」，不区分具体原因
- 会话默认 12 小时过期

## 切回其他模式

```bash
# 免登录（单机自用）
kubectl -n vla-platform set env deploy/vla-web PLATFORM_AUTH_MODE=off

# 纯令牌（机器调用）
kubectl -n vla-platform set env deploy/vla-web PLATFORM_AUTH_MODE=token
```

切完都要 `kubectl -n vla-platform rollout restart deploy/vla-web`。
