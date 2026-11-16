import {
  App,
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  UndoOutlined,
} from '@ant-design/icons';
import { useMemo, useState } from 'react';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError, isForbidden } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { ForbiddenPage } from '../components/ForbiddenPage';
import { useAuth } from '../context/AuthContext';
import { usePolling } from '../hooks/usePolling';
import type { QuotaUpdate, QuotaUpdateResponse, UserCreateRequest, UserSummary, UserUpdateRequest } from '../types/api';
import { formatDateTime } from '../utils/format';

const ROLE_LABELS: Record<string, string> = { admin: '管理员', user: '普通用户' };
const SOURCE_LABELS: Record<string, string> = { off: '匿名', token: '共享令牌', ldap: 'LDAP', local: '本地账号' };

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
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editingUser, setEditingUser] = useState<UserSummary | null>(null);
  const [updatingUser, setUpdatingUser] = useState(false);
  const [createForm] = Form.useForm<UserCreateRequest>();
  const [editForm] = Form.useForm<UserUpdateRequest>();

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

  const handleCreateUser = async () => {
    try {
      const values = await createForm.validateFields();
      setCreating(true);
      await api.createUser({
        username: values.username.trim(),
        password: values.password,
        role: values.role,
        max_gpus: values.max_gpus ?? null,
        max_active_jobs: values.max_active_jobs ?? null,
      });
      message.success(`已添加用户 ${values.username}`);
      setCreateOpen(false);
      createForm.resetFields();
      refresh();
    } catch (reason) {
      if (reason && typeof reason === 'object' && 'errorFields' in reason) return;
      message.error(getErrorMessage(reason));
    } finally {
      setCreating(false);
    }
  };

  const openEditModal = (user: UserSummary) => {
    setEditingUser(user);
    editForm.setFieldsValue({ role: user.role === 'admin' ? 'admin' : 'user', password: '' });
  };

  const handleUpdateUser = async () => {
    if (!editingUser) return;
    try {
      const values = await editForm.validateFields();
      const payload: UserUpdateRequest = { role: values.role };
      if (values.password && values.password.trim()) {
        payload.password = values.password;
      }
      setUpdatingUser(true);
      await api.updateUser(editingUser.username, payload);
      message.success(`已更新用户 ${editingUser.username}`);
      setEditingUser(null);
      editForm.resetFields();
      refresh();
    } catch (reason) {
      if (reason && typeof reason === 'object' && 'errorFields' in reason) return;
      message.error(getErrorMessage(reason));
    } finally {
      setUpdatingUser(false);
    }
  };

  const handleDeleteUser = async (username: string) => {
    try {
      await api.deleteUser(username);
      message.success(`已删除用户 ${username}`);
      refresh();
    } catch (reason) {
      message.error(getErrorMessage(reason));
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
        width: 260,
        render: (_, user) => {
          const dirty = isDirty(draftFor(user), user);
          const isLocal = user.source === 'local';
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
              {isLocal && (
                <>
                  <Button
                    type="link"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => openEditModal(user)}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title={`确定删除用户 ${user.username}？`}
                    onConfirm={() => void handleDeleteUser(user.username)}
                  >
                    <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>
                </>
              )}
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
          <Typography.Text type="secondary">添加/管理平台用户、查看实时资源占用，并调整每用户的 GPU 与活跃任务上限</Typography.Text>
        </div>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            添加用户
          </Button>
          <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        </Space>
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
          scroll={{ x: 1280 }}
          pagination={false}
          locale={{ emptyText: '暂无用户记录' }}
        />
      </Card>

      <Modal
        title="添加用户"
        open={createOpen}
        onOk={() => void handleCreateUser()}
        onCancel={() => {
          setCreateOpen(false);
          createForm.resetFields();
        }}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
      >
        <Form form={createForm} layout="vertical" initialValues={{ role: 'user' }}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/, message: '仅支持字母、数字、点、下划线和连字符' },
            ]}
          >
            <Input placeholder="例如 alice" />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[{ required: true, message: '请输入初始密码' }]}
          >
            <Input.Password placeholder="请输入初始密码" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'user', label: '普通用户' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
          <Space style={{ width: '100%' }} size={16}>
            <Form.Item name="max_gpus" label="GPU 配额（留空不限）">
              <InputNumber min={0} precision={0} placeholder="不限" style={{ width: 200 }} />
            </Form.Item>
            <Form.Item name="max_active_jobs" label="活跃任务配额（留空不限）">
              <InputNumber min={0} precision={0} placeholder="不限" style={{ width: 200 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title={editingUser ? `编辑用户：${editingUser.username}` : '编辑用户'}
        open={Boolean(editingUser)}
        onOk={() => void handleUpdateUser()}
        onCancel={() => {
          setEditingUser(null);
          editForm.resetFields();
        }}
        confirmLoading={updatingUser}
        okText="保存"
        cancelText="取消"
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'user', label: '普通用户' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
          <Form.Item name="password" label="重置密码（留空则不修改）">
            <Input.Password placeholder="留空则保持原密码" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
