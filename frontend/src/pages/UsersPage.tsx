import { App, Alert, Button, Card, InputNumber, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError, isForbidden } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { ForbiddenPage } from '../components/ForbiddenPage';
import { useAuth } from '../context/AuthContext';
import { usePolling } from '../hooks/usePolling';
import type { QuotaUpdate, QuotaUpdateResponse, UserSummary } from '../types/api';
import { formatDateTime } from '../utils/format';

const ROLE_LABELS: Record<string, string> = { admin: '管理员', user: '普通用户' };
const SOURCE_LABELS: Record<string, string> = { off: '匿名', token: '共享令牌', ldap: 'LDAP' };

type QuotaField = keyof QuotaUpdate;

interface QuotaDraft {
  max_gpus: number | null;
  max_active_jobs: number | null;
}

// InputNumber 清空时给出 null，恰好表示「不限」；非法输入统一回退为不限
function toDraft(value: UserSummary): QuotaDraft {
  return {
    max_gpus: value.quota?.max_gpus ?? null,
    max_active_jobs: value.quota?.max_active_jobs ?? null,
  };
}

function isDirty(draft: QuotaDraft, value: UserSummary): boolean {
  const current = toDraft(value);
  return draft.max_gpus !== current.max_gpus || draft.max_active_jobs !== current.max_active_jobs;
}

export function UsersPage() {
  const { isAdmin } = useAuth();
  const { message } = App.useApp();
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, QuotaDraft>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});

  const refresh = usePolling(async (signal) => {
    try {
      const data = await api.users(signal);
      setUsers(data.items);
      setError(null);
    } catch (reason) {
      if (isAbortError(reason)) return;
      if (isForbidden(reason)) {
        setForbidden(true);
        return;
      }
      setError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, 15000);

  const draftFor = (user: UserSummary): QuotaDraft => drafts[user.username] ?? toDraft(user);

  const updateDraft = (user: UserSummary, field: QuotaField, value: number | null) => {
    setDrafts((previous) => {
      const base = previous[user.username] ?? toDraft(user);
      return { ...previous, [user.username]: { ...base, [field]: value } };
    });
  };

  // 把 PUT 响应合并回列表：配额与实时用量以服务端为准，其余字段保持不变
  const mergeQuota = (updated: QuotaUpdateResponse) => {
    setUsers((previous) =>
      previous.map((item) =>
        item.username === updated.username
          ? {
              ...item,
              quota: {
                max_gpus: updated.quota?.max_gpus ?? null,
                max_active_jobs: updated.quota?.max_active_jobs ?? null,
              },
              gpu_in_use: updated.quota?.gpu_in_use ?? item.gpu_in_use,
              active_jobs: updated.quota?.active_jobs ?? item.active_jobs,
            }
          : item,
      ),
    );
  };

  const resetDraft = (username: string) => {
    setDrafts((previous) => {
      const next = { ...previous };
      delete next[username];
      return next;
    });
  };

  const saveQuota = async (user: UserSummary) => {
    const draft = draftFor(user);
    setSaving((previous) => ({ ...previous, [user.username]: true }));
    try {
      // 响应形状：{"username","quota":{"max_gpus","max_active_jobs","gpu_in_use","active_jobs"}}
      const updated = await api.putQuota(user.username, {
        max_gpus: draft.max_gpus,
        max_active_jobs: draft.max_active_jobs,
      });
      message.success(`用户 ${user.username} 的配额已更新`);
      mergeQuota(updated);
      resetDraft(user.username);
    } catch (reason) {
      message.error(getErrorMessage(reason));
    } finally {
      setSaving((previous) => {
        const next = { ...previous };
        delete next[user.username];
        return next;
      });
    }
  };

  const columns = useMemo<ColumnsType<UserSummary>>(
    () => [
      { title: '用户名', dataIndex: 'username', key: 'username', fixed: 'left', width: 160 },
      {
        title: '角色',
        dataIndex: 'role',
        key: 'role',
        width: 100,
        render: (role: string) => <Tag color={role === 'admin' ? 'blue' : 'default'}>{ROLE_LABELS[role] || role}</Tag>,
      },
      {
        title: '来源',
        dataIndex: 'source',
        key: 'source',
        width: 110,
        render: (source: string) => SOURCE_LABELS[source] || source || '—',
      },
      {
        title: '最后活跃',
        dataIndex: 'last_seen_at',
        key: 'last_seen_at',
        width: 180,
        render: formatDateTime,
      },
      { title: '活跃任务', dataIndex: 'active_jobs', key: 'active_jobs', width: 100 },
      {
        title: 'GPU 占用',
        dataIndex: 'gpu_in_use',
        key: 'gpu_in_use',
        width: 100,
        render: (value: number) => `${value ?? 0} 卡`,
      },
      {
        title: 'GPU 配额',
        key: 'max_gpus',
        width: 150,
        render: (_, user) => (
          <InputNumber
            aria-label={`${user.username} 的 GPU 配额`}
            min={0}
            precision={0}
            placeholder="不限"
            value={draftFor(user).max_gpus}
            onChange={(value) => updateDraft(user, 'max_gpus', value ?? null)}
          />
        ),
      },
      {
        title: '活跃任务配额',
        key: 'max_active_jobs',
        width: 150,
        render: (_, user) => (
          <InputNumber
            aria-label={`${user.username} 的活跃任务配额`}
            min={0}
            precision={0}
            placeholder="不限"
            value={draftFor(user).max_active_jobs}
            onChange={(value) => updateDraft(user, 'max_active_jobs', value ?? null)}
          />
        ),
      },
      {
        title: '操作',
        key: 'actions',
        fixed: 'right',
        width: 170,
        render: (_, user) => {
          const dirty = isDirty(draftFor(user), user);
          return (
            <Space size={4}>
              <Button
                type="link"
                size="small"
                icon={<SaveOutlined />}
                disabled={!dirty}
                loading={Boolean(saving[user.username])}
                onClick={() => void saveQuota(user)}
              >
                保存
              </Button>
              <Button
                type="link"
                size="small"
                icon={<UndoOutlined />}
                disabled={!dirty}
                onClick={() => resetDraft(user.username)}
              >
                重置
              </Button>
            </Space>
          );
        },
      },
    ],
    // 列内的配额输入框读取 users/drafts，保存按钮读取 saving，任一变化都需要重建列
    [users, drafts, saving],
  );

  if (!isAdmin || forbidden) {
    return <ForbiddenPage />;
  }

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>用户与配额</Typography.Title>
          <Typography.Text type="secondary">查看平台用户、实时资源占用，并调整每用户的 GPU 与活跃任务上限</Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </div>
      <Alert
        type="info"
        showIcon
        className="section-card"
        message="配额说明"
        description="留空表示不限；修改后该用户下一次提交任务时立即生效，已有任务不受影响。"
      />
      <ErrorAlert error={error} onRetry={refresh} title="用户列表加载失败" />
      <Card>
        <Table<UserSummary>
          rowKey="username"
          columns={columns}
          dataSource={users}
          loading={loading && users.length === 0}
          scroll={{ x: 1200 }}
          pagination={false}
          locale={{ emptyText: '暂无用户记录' }}
        />
      </Card>
    </div>
  );
}