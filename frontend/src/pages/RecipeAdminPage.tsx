import { Alert, App, Button, Card, Modal, Popconfirm, Space, Table, Tag, Typography } from 'antd';
import { EditOutlined, HistoryOutlined, ReloadOutlined, UndoOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useState } from 'react';
import type { ColumnsType } from 'antd/es/table';

import { api, getErrorMessage, isAbortError, isForbidden } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { ForbiddenPage } from '../components/ForbiddenPage';
import { useAuth } from '../context/AuthContext';
import type { AdminRecipe, RecipeHistoryEntry } from '../types/api';
import { formatDateTime } from '../utils/format';

const SOURCE_TAGS: Record<string, { color: string; label: string }> = {
  builtin: { color: 'default', label: '默认' },
  override: { color: 'orange', label: '已覆盖' },
  error: { color: 'red', label: '配置错误' },
};

const ACTION_LABELS: Record<string, string> = {
  save: '保存覆盖',
  restore: '恢复默认',
  rollback: '回滚',
};

interface EditorState {
  id: string;
  yaml: string;
  saving: boolean;
  // 422 的校验错误直接展示在编辑框上方，管理员改完再存
  error: string | null;
}

interface HistoryState {
  id: string;
  items: RecipeHistoryEntry[];
  loading: boolean;
  rollingBack: boolean;
}

export function RecipeAdminPage() {
  const { isAdmin } = useAuth();
  const { message } = App.useApp();
  const [recipes, setRecipes] = useState<AdminRecipe[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [history, setHistory] = useState<HistoryState | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.adminListRecipes();
      setRecipes(data.items ?? []);
      setError(null);
    } catch (reason) {
      if (isAbortError(reason)) return;
      if (isForbidden(reason)) {
        setForbidden(true);
        return;
      }
      setError(getErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openEditor = async (id: string) => {
    try {
      const detail = await api.adminGetRecipe(id);
      setEditor({ id, yaml: detail.yaml, saving: false, error: null });
    } catch (reason) {
      message.error(getErrorMessage(reason));
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    setEditor({ ...editor, saving: true, error: null });
    try {
      await api.adminSaveRecipe(editor.id, editor.yaml);
      message.success(`配方 ${editor.id} 已保存，后续提交立即生效`);
      setEditor(null);
      void load();
    } catch (reason) {
      // 校验失败不关闭弹窗，错误原因留在编辑框上方
      setEditor({ ...editor, saving: false, error: getErrorMessage(reason) });
    }
  };

  const restoreDefault = async (id: string) => {
    try {
      await api.adminRestoreRecipe(id);
      message.success(`配方 ${id} 已恢复内置默认`);
      void load();
    } catch (reason) {
      message.error(getErrorMessage(reason));
    }
  };

  const openHistory = async (id: string) => {
    setHistory({ id, items: [], loading: true, rollingBack: false });
    try {
      const data = await api.adminRecipeHistory(id);
      setHistory({ id, items: data.items ?? [], loading: false, rollingBack: false });
    } catch (reason) {
      setHistory(null);
      message.error(getErrorMessage(reason));
    }
  };

  const rollback = async () => {
    if (!history) return;
    setHistory({ ...history, rollingBack: true });
    try {
      await api.adminRollbackRecipe(history.id);
      message.success(`配方 ${history.id} 已回滚到上一个版本`);
      setHistory(null);
      void load();
    } catch (reason) {
      message.error(getErrorMessage(reason));
      setHistory({ ...history, rollingBack: false });
    }
  };

  const columns: ColumnsType<AdminRecipe> = [
    {
      title: '配方',
      key: 'recipe',
      render: (_, recipe) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{recipe.name}</Typography.Text>
          <Typography.Text type="secondary" code>{recipe.id}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '镜像',
      dataIndex: 'image',
      key: 'image',
      render: (image: string) => (image ? <Typography.Text code>{image}</Typography.Text> : '—'),
    },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 100,
      render: (source: string) => {
        const tag = SOURCE_TAGS[source] ?? { color: 'default', label: source };
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: '最近修改',
      key: 'updated',
      width: 220,
      render: (_, recipe) =>
        recipe.updated_at ? (
          <Space direction="vertical" size={0}>
            <span>{formatDateTime(recipe.updated_at)}</span>
            <Typography.Text type="secondary">{recipe.updated_by || '—'}</Typography.Text>
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 230,
      render: (_, recipe) => (
        <Space size={4}>
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => void openEditor(recipe.id)}>
            编辑 YAML
          </Button>
          <Button type="link" size="small" icon={<HistoryOutlined />} onClick={() => void openHistory(recipe.id)}>
            历史
          </Button>
          <Popconfirm
            title={`恢复 ${recipe.id} 为内置默认？`}
            description="删除管理员覆盖，立即生效；历史记录仍保留，可再回滚。"
            onConfirm={() => void restoreDefault(recipe.id)}
          >
            <Button
              type="link"
              size="small"
              icon={<UndoOutlined />}
              disabled={recipe.source !== 'override'}
            >
              恢复默认
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (!isAdmin || forbidden) {
    return <ForbiddenPage />;
  }

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>配方管理</Typography.Title>
          <Typography.Text type="secondary">
            以完整 YAML 覆盖内置训练配方，保存即生效；内置默认随镜像发布，不受覆盖影响
          </Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
      </div>
      <Alert
        type="info"
        showIcon
        className="section-card"
        message="覆盖保存在平台数据库，不受 Web 容器重启或 /app/recipes 只读影响"
        description="保存前会做完整校验（YAML 语法、配方字段、镜像名、参数模板），不合法的修改不会落库。误改可在「历史」里回滚。"
      />
      <ErrorAlert error={error} onRetry={() => void load()} title="配方加载失败" />
      <Card>
        <Table<AdminRecipe>
          rowKey="id"
          columns={columns}
          dataSource={recipes}
          loading={loading && recipes.length === 0}
          scroll={{ x: 1000 }}
          pagination={false}
          locale={{ emptyText: '还没有登记任何训练配方' }}
        />
      </Card>

      <Modal
        title={editor ? `编辑配方：${editor.id}` : ''}
        open={editor !== null}
        width={860}
        okText="保存"
        cancelText="取消"
        confirmLoading={editor?.saving ?? false}
        onOk={() => void saveEditor()}
        onCancel={() => setEditor(null)}
        destroyOnHidden
      >
        {editor && (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {editor.error && <Alert type="error" showIcon message="保存失败" description={editor.error} />}
            <Typography.Text type="secondary">
              完整 YAML；id 必须保持为 {editor.id}，其余字段均可调整。
            </Typography.Text>
            <textarea
              aria-label="配方 YAML"
              className="yaml-editor"
              value={editor.yaml}
              onChange={(event) => setEditor({ ...editor, yaml: event.target.value })}
            />
          </Space>
        )}
      </Modal>

      <Modal
        title={history ? `变更历史：${history.id}` : ''}
        open={history !== null}
        footer={null}
        onCancel={() => setHistory(null)}
      >
        {history && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {history.items.length === 0 && !history.loading && (
              <Typography.Text type="secondary">还没有任何修改记录。</Typography.Text>
            )}
            {history.items.map((item) => (
              <div key={item.id} className="recipe-history-item">
                <Tag color={item.action === 'save' ? 'orange' : 'blue'}>
                  {ACTION_LABELS[item.action] || item.action}
                </Tag>
                <span>{formatDateTime(item.changed_at)}</span>
                <Typography.Text type="secondary">{item.changed_by}</Typography.Text>
              </div>
            ))}
            <Popconfirm
              title={`回滚 ${history.id} 到上一个版本？`}
              description="连续回滚会沿版本链逐步倒退，最终回到内置默认。"
              onConfirm={() => void rollback()}
            >
              <Button
                danger
                icon={<UndoOutlined />}
                loading={history.rollingBack}
                disabled={history.items.length === 0}
              >
                回滚到上一个版本
              </Button>
            </Popconfirm>
          </Space>
        )}
      </Modal>
    </div>
  );
}
