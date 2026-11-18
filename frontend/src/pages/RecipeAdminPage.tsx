import {
  Alert,
  App,
  Button,
  Card,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { DeleteOutlined, EditOutlined, HistoryOutlined, PlusOutlined, ReloadOutlined, UndoOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useState } from 'react';
import type { ColumnsType } from 'antd/es/table';

import { api, getErrorMessage, isAbortError, isForbidden } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { ForbiddenPage } from '../components/ForbiddenPage';
import { useAuth } from '../context/AuthContext';
import type { AdminRecipe, AdminRecipeFields, AdminRecipeParamField, RecipeHistoryEntry, RobotProfile } from '../types/api';
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
  fields: AdminRecipeFields | null;
  mode: 'form' | 'yaml';
  paramValues: Record<string, string | number | boolean | null>;
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
  const [form] = Form.useForm();
  const [robots, setRobots] = useState<RobotProfile[]>([]);
  const [robotsLoading, setRobotsLoading] = useState(false);
  const [robotModal, setRobotModal] = useState<{ mode: 'create' | 'edit'; id?: string; saving: boolean } | null>(null);
  const [robotForm] = Form.useForm();
  const [cloneModal, setCloneModal] = useState<{ saving: boolean } | null>(null);
  const [cloneForm] = Form.useForm<{ id: string; clone_from: string; name?: string }>();

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

  const loadRobots = useCallback(async () => {
    if (typeof api.listRobots !== 'function') return;
    setRobotsLoading(true);
    try {
      const data = await api.listRobots();
      setRobots(data.items ?? []);
    } catch {
      // ignore if mocked without listRobots
    } finally {
      setRobotsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadRobots();
  }, [load, loadRobots]);

  const openRobotModal = (mode: 'create' | 'edit', robot?: RobotProfile) => {
    if (mode === 'edit' && robot) {
      robotForm.setFieldsValue({
        id: robot.id,
        name: robot.name,
        description: robot.description,
        camera_keywords: (robot.camera_keywords ?? []).join(', '),
        param_defaults: Object.entries(robot.param_defaults ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
        camera_top: robot.camera_top ?? "observation.images.faceImg",
        camera_left: robot.camera_left ?? "observation.images.leftImg",
        camera_right: robot.camera_right ?? "observation.images.rightImg",
        state_dim: robot.state_dim ?? 20,
        action_dim: robot.action_dim ?? 20,
        sort_order: robot.sort_order ?? 100,
      });
      setRobotModal({ mode: 'edit', id: robot.id, saving: false });
    } else {
      robotForm.setFieldsValue({ id: '', name: '', description: '', camera_keywords: '', param_defaults: '', camera_top: 'observation.images.faceImg', camera_left: 'observation.images.leftImg', camera_right: 'observation.images.rightImg', state_dim: 20, action_dim: 20, sort_order: 100 });
      setRobotModal({ mode: 'create', saving: false });
    }
  };

  const saveRobotModal = async () => {
    if (!robotModal) return;
    try {
      const values = await robotForm.validateFields();
      setRobotModal({ ...robotModal, saving: true });
      const cameraKeywords = String(values.camera_keywords || '')
        .split(/[,，\n]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const paramDefaults: Record<string, string> = {};
      for (const line of String(values.param_defaults || '').split(/\n+/)) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.includes('=')) continue;
        const idx = trimmed.indexOf('=');
        const k = trimmed.slice(0, idx).trim();
        const v = trimmed.slice(idx + 1).trim();
        if (k) paramDefaults[k] = v;
      }
      if (robotModal.mode === 'create') {
        await api.adminCreateRobot({
          id: String(values.id).trim(),
          name: String(values.name).trim(),
          description: String(values.description || '').trim(),
          camera_keywords: cameraKeywords,
          param_defaults: paramDefaults,
          camera_top: String(values.camera_top || "").trim(),
          camera_left: String(values.camera_left || "").trim(),
          camera_right: String(values.camera_right || "").trim(),
          state_dim: Number(values.state_dim ?? 20),
          action_dim: Number(values.action_dim ?? 20),
          sort_order: Number(values.sort_order ?? 100),
        });
        message.success(`机器人 ${values.name} 已添加`);
      } else if (robotModal.id) {
        await api.adminUpdateRobot(robotModal.id, {
          name: String(values.name).trim(),
          description: String(values.description || '').trim(),
          camera_keywords: cameraKeywords,
          param_defaults: paramDefaults,
          camera_top: String(values.camera_top || "").trim(),
          camera_left: String(values.camera_left || "").trim(),
          camera_right: String(values.camera_right || "").trim(),
          state_dim: Number(values.state_dim ?? 20),
          action_dim: Number(values.action_dim ?? 20),
          sort_order: Number(values.sort_order ?? 100),
        });
        message.success(`机器人 ${values.name} 已更新`);
      }
      setRobotModal(null);
      void loadRobots();
    } catch (reason) {
      if (reason && typeof reason === 'object' && 'errorFields' in reason) {
        setRobotModal((prev) => (prev ? { ...prev, saving: false } : prev));
        return;
      }
      message.error(getErrorMessage(reason));
      setRobotModal((prev) => (prev ? { ...prev, saving: false } : prev));
    }
  };

  const deleteRobot = async (id: string) => {
    try {
      await api.adminDeleteRobot(id);
      message.success(`已删除机器人 ${id}`);
      void loadRobots();
    } catch (reason) {
      message.error(getErrorMessage(reason));
    }
  };

  const robotColumns: ColumnsType<RobotProfile> = [
    {
      title: '机器人',
      key: 'robot',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{r.name}</Typography.Text>
          <Typography.Text type="secondary" code>{r.id}</Typography.Text>
        </Space>
      ),
    },
    { title: '说明', dataIndex: 'description', key: 'description' },
    {
      title: '相机匹配关键词',
      dataIndex: 'camera_keywords',
      key: 'camera_keywords',
      render: (kws: string[]) => (kws && kws.length > 0 ? kws.map((k) => <Tag key={k}>{k}</Tag>) : '全部'),
    },
    {
      title: '关联默认参数',
      dataIndex: 'param_defaults',
      key: 'param_defaults',
      render: (pd: Record<string, string>) => (
        <Space direction="vertical" size={0}>
          {Object.entries(pd || {}).map(([k, v]) => (
            <Typography.Text key={k} code style={{ fontSize: 12 }}>{`${k}=${v}`}</Typography.Text>
          ))}
        </Space>
      ),
    },
    { title: '排序', dataIndex: 'sort_order', key: 'sort_order', width: 80 },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, r) => (
        <Space size={4}>
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => openRobotModal('edit', r)}>
            编辑
          </Button>
          <Popconfirm title={`确定删除机器人 ${r.name}？`} onConfirm={() => void deleteRobot(r.id)}>
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const openEditor = async (id: string) => {
    try {
      const detail = await api.adminGetRecipe(id);
      const fields = detail.fields ?? null;
      const paramValues: Record<string, string | number | boolean | null> = {};
      if (fields?.params) {
        for (const p of fields.params) {
          const val = p.default ?? fields.defaults?.[p.key] ?? null;
          paramValues[p.key] = val;
        }
      }
      if (fields) {
        form.setFieldsValue({
          name: fields.name,
          description: fields.description,
          image: fields.image,
          entrypoint: fields.entrypoint,
          gpu_count: fields.gpu_count,
          estimated_vram_gb: fields.estimated_vram_gb ?? null,
          public: fields.public ?? true,
          env_text: Object.entries(fields.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"),
          host_paths_text: (fields.host_paths ?? []).map((hp) => `${hp.name}|${hp.host_path}|${hp.mount_path}|${hp.read_only ? "ro" : "rw"}`).join("\n"),
        });
      }
      setEditor({
        id,
        yaml: detail.yaml,
        fields,
        mode: fields ? 'form' : 'yaml',
        paramValues,
        saving: false,
        error: null,
      });
    } catch (reason) {
      message.error(getErrorMessage(reason));
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    setEditor({ ...editor, saving: true, error: null });
    try {
      if (editor.mode === 'form' && editor.fields) {
        const values = await form.validateFields();
        const envMap: Record<string, string> = {};
        for (const line of String(values.env_text || "").split(/\n+/)) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.includes("=")) continue;
          const idx = trimmed.indexOf("=");
          const k = trimmed.slice(0, idx).trim();
          const v = trimmed.slice(idx + 1).trim();
          if (k) envMap[k] = v;
        }
        const hostPathsList: Array<{ name: string; host_path: string; mount_path: string; read_only: boolean }> = [];
        for (const line of String(values.host_paths_text || "").split(/\n+/)) {
          const parts = line.split("|").map((s) => s.trim());
          if (parts.length >= 3 && parts[0] && parts[1] && parts[2]) {
            hostPathsList.push({
              name: parts[0],
              host_path: parts[1],
              mount_path: parts[2],
              read_only: (parts[3] || "ro").toLowerCase() !== "rw",
            });
          }
        }
        const { env_text: _et, host_paths_text: _ht, ...restValues } = values;
        await api.adminSaveRecipe(editor.id, {
          fields: {
            ...restValues,
            env: envMap,
            host_paths: hostPathsList,
            param_defaults: editor.paramValues,
          },
        });
      } else {
        await api.adminSaveRecipe(editor.id, editor.yaml);
      }
      message.success(`配方 ${editor.id} 已保存，后续提交立即生效`);
      setEditor(null);
      void load();
    } catch (reason) {
      if (reason && typeof reason === 'object' && 'errorFields' in reason) {
        setEditor({ ...editor, saving: false });
        return;
      }
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

  const renderParamInput = (param: AdminRecipeParamField) => {
    if (!editor) return null;
    const value = editor.paramValues[param.key] ?? null;
    const updateVal = (next: string | number | boolean | null) =>
      setEditor((prev) =>
        prev ? { ...prev, paramValues: { ...prev.paramValues, [param.key]: next } } : prev,
      );

    if (param.type === 'enum' && param.options && param.options.length > 0) {
      return (
        <Select
          value={value === null || value === undefined ? undefined : String(value)}
          onChange={(v) => updateVal(v)}
          options={param.options.map((opt) => ({ value: opt, label: opt }))}
        />
      );
    }
    if (param.type === 'bool') {
      return (
        <Switch
          checked={Boolean(value)}
          onChange={(checked) => updateVal(checked)}
        />
      );
    }
    if (param.type === 'int' || param.type === 'float') {
      return (
        <InputNumber
          style={{ width: '100%' }}
          min={param.min}
          max={param.max}
          precision={param.type === 'int' ? 0 : undefined}
          value={typeof value === 'number' ? value : value !== null && value !== '' ? Number(value) : null}
          onChange={(v) => updateVal(v)}
        />
      );
    }
    return (
      <Input
        value={value === null || value === undefined ? '' : String(value)}
        onChange={(e) => updateVal(e.target.value)}
      />
    );
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
            编辑
          </Button>
          <Popconfirm
            title={`恢复 ${recipe.id} 为内置默认？`}
            description="会删除当前覆盖，后续提交重新使用内置配方；历史里仍可回滚。"
            disabled={recipe.source === 'builtin'}
            onConfirm={() => void restoreDefault(recipe.id)}
          >
            <Button type="link" size="small" disabled={recipe.source === 'builtin'}>
              恢复默认
            </Button>
          </Popconfirm>
          <Button type="link" size="small" icon={<HistoryOutlined />} onClick={() => void openHistory(recipe.id)}>
            历史
          </Button>
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
            可视化修改配方默认参数与运行配置，保存即生效；内置默认随镜像发布，不受覆盖影响
          </Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
      </div>
      <ErrorAlert error={error} onRetry={() => void load()} title="配方加载失败" />
      <Tabs
        defaultActiveKey="recipes"
        items={[
          {
            key: 'recipes',
            label: '训练配方',
            children: (
              <Card
                extra={
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => {
                      cloneForm.setFieldsValue({ id: "", clone_from: recipes[0]?.id || "", name: "" });
                      setCloneModal({ saving: false });
                    }}
                  >
                    克隆 / 新建配方
                  </Button>
                }
              >
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
            ),
          },
          {
            key: 'robots',
            label: '机器人配置',
            children: (
              <Card
                extra={
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => openRobotModal('create')}>
                    添加机器人
                  </Button>
                }
              >
                <Table<RobotProfile>
                  rowKey="id"
                  columns={robotColumns}
                  dataSource={robots}
                  loading={robotsLoading && robots.length === 0}
                  pagination={false}
                  locale={{ emptyText: '还没有配置任何机器人' }}
                />
              </Card>
            ),
          },
        ]}
      />

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
            <Tabs
              activeKey={editor.mode}
              onChange={(key) => setEditor({ ...editor, mode: key as 'form' | 'yaml' })}
              items={[
                ...(editor.fields
                  ? [
                      {
                        key: 'form',
                        label: '表单编辑',
                        children: (
                          <Form form={form} layout="vertical">
                            <Form.Item name="name" label="配方名称" rules={[{ required: true, message: '请输入配方名称' }]}>
                              <Input />
                            </Form.Item>
                            <Form.Item name="description" label="配方说明">
                              <Input.TextArea rows={2} />
                            </Form.Item>
                            <Form.Item name="image" label="训练镜像" rules={[{ required: true, message: '请输入镜像名' }]}>
                              <Input />
                            </Form.Item>
                            <Form.Item name="entrypoint" label="入口命令" rules={[{ required: true, message: '请输入入口命令' }]}>
                              <Input.TextArea rows={2} />
                            </Form.Item>
                            <Space size={16} wrap>
                              <Form.Item name="gpu_count" label="默认 GPU 数" rules={[{ required: true }]}>
                                <InputNumber min={1} max={64} precision={0} />
                              </Form.Item>
                              <Form.Item name="estimated_vram_gb" label="预估显存 (GB)">
                                <InputNumber min={1} precision={0} placeholder="可选" />
                              </Form.Item>
                              <Form.Item name="public" label="对普通用户公开" valuePropName="checked">
                                <Switch />
                              </Form.Item>
                            </Space>
                            <Divider orientation="left" plain>环境变量与宿主机挂载</Divider>
                            <Form.Item name="env_text" label="环境变量（每行一个 KEY=VALUE）">
                              <Input.TextArea rows={3} placeholder="HF_HUB_OFFLINE=1" />
                            </Form.Item>
                            <Form.Item
                              name="host_paths_text"
                              label="宿主机挂载（每行一个 name|host_path|mount_path|ro或rw）"
                            >
                              <Input.TextArea rows={3} placeholder="models|/models|/models|ro" />
                            </Form.Item>
                            {editor.fields.params && editor.fields.params.length > 0 && (
                              <>
                                <Divider orientation="left" plain>默认训练参数</Divider>
                                {editor.fields.params.map((param) => (
                                  <Form.Item
                                    key={param.key}
                                    label={param.label || param.key}
                                    extra={param.help}
                                  >
                                    {renderParamInput(param)}
                                  </Form.Item>
                                ))}
                              </>
                            )}
                          </Form>
                        ),
                      },
                    ]
                  : []),
                {
                  key: 'yaml',
                  label: 'YAML 原文',
                  children: (
                    <Space direction="vertical" size={8} style={{ width: '100%' }}>
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
                  ),
                },
              ]}
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

      <Modal
        title={robotModal?.mode === 'edit' ? `编辑机器人：${robotModal.id}` : '添加机器人'}
        open={robotModal !== null}
        okText="保存"
        cancelText="取消"
        confirmLoading={robotModal?.saving ?? false}
        onOk={() => void saveRobotModal()}
        onCancel={() => setRobotModal(null)}
        destroyOnHidden
      >
        <Form form={robotForm} layout="vertical">
          <Form.Item
            name="id"
            label="机器人 ID"
            rules={[
              { required: true, message: '请输入机器人 ID' },
              { pattern: /^[a-z0-9][a-z0-9_-]{0,47}$/, message: '仅支持小写字母、数字、下划线和中划线' },
            ]}
          >
            <Input disabled={robotModal?.mode === 'edit'} placeholder="例如 franka" />
          </Form.Item>
          <Form.Item name="name" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input placeholder="例如 Franka Emika" />
          </Form.Item>
          <Form.Item name="description" label="相机/维度说明">
            <Input placeholder="例如 7 轴单臂 + 腕部相机" />
          </Form.Item>
          <Form.Item
            name="camera_keywords"
            label="数据集相机匹配关键词（逗号分隔，留空匹配全部）"
          >
            <Input placeholder="例如 faceImg, wrist" />
          </Form.Item>
          <Form.Item
            name="param_defaults"
            label="关联默认参数（每行一个 key=value）"
          >
            <Input.TextArea
              rows={4}
              placeholder={"openpi_config_name=pi05_turtle\ndata_name=robam_x2robot"}
            />
          </Form.Item>
          <Space size={12} wrap>
            <Form.Item name="state_dim" label="State 维度">
              <InputNumber min={1} max={256} precision={0} />
            </Form.Item>
            <Form.Item name="action_dim" label="Action 维度">
              <InputNumber min={1} max={256} precision={0} />
            </Form.Item>
          </Space>
          <Form.Item name="camera_top" label="主视角相机键名（自动生成训练配置）">
            <Input placeholder="observation.images.faceImg" />
          </Form.Item>
          <Form.Item name="camera_left" label="左腕相机键名">
            <Input placeholder="observation.images.leftImg" />
          </Form.Item>
          <Form.Item name="camera_right" label="右腕相机键名">
            <Input placeholder="observation.images.rightImg" />
          </Form.Item>
          <Form.Item name="sort_order" label="排序（越小越靠前）">
            <InputNumber min={0} max={9999} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="克隆 / 新建配方"
        open={cloneModal !== null}
        okText="创建并编辑"
        cancelText="取消"
        confirmLoading={cloneModal?.saving ?? false}
        onOk={async () => {
          try {
            const values = await cloneForm.validateFields();
            setCloneModal({ saving: true });
            await api.adminCreateRecipe({
              id: values.id.trim(),
              clone_from: values.clone_from,
              ...(values.name?.trim() ? { name: values.name.trim() } : {}),
            });
            message.success(`配方 ${values.id} 已创建`);
            setCloneModal(null);
            await load();
            void openEditor(values.id.trim());
          } catch (reason) {
            if (reason && typeof reason === 'object' && 'errorFields' in reason) {
              setCloneModal({ saving: false });
              return;
            }
            message.error(getErrorMessage(reason));
            setCloneModal({ saving: false });
          }
        }}
        onCancel={() => setCloneModal(null)}
        destroyOnHidden
      >
        <Form form={cloneForm} layout="vertical">
          <Form.Item
            name="id"
            label="新配方 ID"
            rules={[
              { required: true, message: "请输入新配方 ID" },
              { pattern: /^[a-z0-9][a-z0-9-]{0,47}$/, message: "仅支持小写字母、数字和中划线" },
            ]}
          >
            <Input placeholder="例如 my-robot-sft" />
          </Form.Item>
          <Form.Item
            name="clone_from"
            label="基于已有配方模板"
            rules={[{ required: true, message: "请选择模板配方" }]}
          >
            <Select options={recipes.map((r) => ({ value: r.id, label: `${r.name} (${r.id})` }))} />
          </Form.Item>
          <Form.Item name="name" label="新配方名称（可选）">
            <Input placeholder="例如 自定义机器人 SFT" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
