# 运维手册：备份、恢复与清理

平台的持久数据分两类，可靠性要求完全不同。

| 数据 | 位置（宿主机） | 能否重建 | 备份方式 |
|---|---|---|---|
| 数据库（审计/历史/配额） | `/data/vla-platform/history/platform.db` | **不能** | 自动 + 手动 |
| 训练产物 | `/data/vla-platform/checkpoints/` | 能（重跑几小时） | 不备份，按需清理 |

数据库是重点：审计日志和提交历史一旦丢失就无法重建。

## 一、路径要分清

容器内和宿主机看到的路径不一样，这是最容易搞错的地方：

| 容器内 | 宿主机 |
|---|---|
| `/data/history/platform.db` | `/data/vla-platform/history/platform.db` |
| `/data/vla-platform/checkpoints` | `/data/vla-platform/checkpoints`（同名） |

在宿主机上跑运维命令时，一律用右列的路径。我曾用容器路径去宿主机上查，
报「文件不存在」，当时以为数据库丢了。

## 二、备份

### 自动

服务启动后会自动起一个守护线程，默认每 24 小时备份一次，
首次在启动后 5 分钟内。保留最近 14 份。

相关配置：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PLATFORM_BACKUP_DIR` | `/data/history/backups` | 容器内路径 |
| `PLATFORM_BACKUP_KEEP` | 14 | 保留份数，至少 1 |
| `PLATFORM_BACKUP_INTERVAL_SECONDS` | 86400 | 0 表示关闭自动备份 |

### 手动

```bash
ADMIN=<管理员令牌>
curl -X POST -H "Authorization: Bearer $ADMIN" \
  http://172.16.122.37:30080/api/v1/maintenance/backups
```

查看备份列表：

```bash
curl -H "Authorization: Bearer $ADMIN" \
  http://172.16.122.37:30080/api/v1/maintenance/backups
```

### 为什么不用 cp

SQLite 在并发写入时，数据库文件本身不一定是自洽快照。直接 `cp` 可能得到
打不开、或者缺最后几个事务的副本——这种「看着有、实际没用」的备份
比明说没有备份更危险。

平台用的是 `sqlite3.Connection.backup()`，在读事务里生成一致快照，
不需要停服务。实测在持续写入的同时备份，副本 `integrity_check` 为 `ok`。

### 备份目录的权限

备份文件属主是容器内的 UID 10001。运维在宿主机上用**普通用户**清理时
会 `Permission denied`——这一点实测过：接口返回「保留 2 份」看起来正常，
实际一份都没删掉。

所以：
- 服务自己做的自动备份清理**是容器内执行的**（同一 UID），不受影响
- 运维手工清理必须用 `sudo`
- `prune_backups` 现在会把失败列在 `failed` 里，不再静默。如果看到这个字段
  非空，说明权限不足

## 三、恢复

### 1. 先确认备份是好的

```bash
sudo python3 scripts/restore_db.py --list
```

输出会逐个标注 OK 或「坏」，并显示关键表的行数：

```
  OK   platform-20261110T012000Z.db   634880 字节  submissions=89  audit_log=105  cancellations=34
```

选中一份后可以先单独检查：

```bash
sudo python3 scripts/restore_db.py --check /data/vla-platform/history/backups/platform-xxx.db
```

### 2. 执行恢复

```bash
sudo python3 scripts/restore_db.py --restore \
  /data/vla-platform/history/backups/platform-xxx.db
```

工具会：
- 拒绝使用检查不通过的备份（加 `--force` 可强制）
- 把当前库另存为 `platform.db.before-restore-<时间>`，万一选错还能退回
- 清掉 `-wal` / `-shm`，避免新库读到不属于它的页
- 原子替换

### 3. 必须重启服务

```bash
kubectl -n vla-platform rollout restart deploy/vla-web
```

**不做热恢复**：SQLite 在进程持有连接时替换文件，旧进程会继续写已删除的
inode，现象是「看着正常但数据全丢」。

## 四、清理训练产物

### 先看占用

```bash
curl -H "Authorization: Bearer $ADMIN" \
  http://172.16.122.37:30080/api/v1/maintenance/checkpoints
```

按大小倒序，一眼看出哪些任务吃掉磁盘。

### 删除

```bash
sudo python3 scripts/safe_cleanup.py --list                        # 列出
sudo python3 scripts/safe_cleanup.py smolvla-pick1028-003          # 预览
sudo python3 scripts/safe_cleanup.py smolvla-pick1028-003 --yes    # 执行
```

**不要用 `rm -rf`。** 我们真的因为一条 `rm -rf` 丢掉了全部训练产物：
一个脚本里变量展开为空，`rm -rf "/checkpoints/checkpoints"` 在容器内
以 root 跑掉了。安全工具会拒绝空值、通配符、路径分隔符和越界路径。

## 五、事故复盘：那次删库

| 环节 | 发生了什么 |
|---|---|
| 直接原因 | 测试脚本里 `rm -rf "$CKPT/checkpoints"`，`$JOB` 展开为空 |
| 执行位置 | pod 内以 root 身份 |
| 后果 | `/data/vla-platform/checkpoints` 下全部产物被清空 |
| 能救回的 | 一个 checkpoint（靠之前手工拷贝的副本） |
| 无法恢复的 | 其余产物与对应的 metrics.jsonl |

之后加的三道防护：
1. `safe_job_dir()`：任何可疑输入直接拒绝，`resolve()` 之后再比对前缀
2. `safe_cleanup.py`：默认只预览，必须精确任务名
3. 数据库备份 + 恢复工具：至少管理数据不再裸奔

**教训**：删除命令里出现变量就要警惕。变量为空时，`rm -rf "/checkpoints/"`
和 `rm -rf "/checkpoints/checkpoints"` 的危险程度完全一样，但前者看起来更正常。
