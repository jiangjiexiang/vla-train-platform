import {
  App,
  Alert,
  AutoComplete,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Typography,
} from 'antd';
import { MinusCircleOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, getErrorMessage, isAbortError, isUnauthorized } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { PARAMS_FIELD, TrainParamsForm } from '../components/TrainParamsForm';
import { useAuth } from '../context/AuthContext';
import { usePolling } from '../hooks/usePolling';
import { loadEffectiveQuota } from '../api/quota';
import type { EffectiveQuota, ImageParamsResponse, Job, ParamsTemplate, Priority, SubmitJobRequest } from '../types/api';
import {
  buildCommandFromTemplate,
  builtinTemplate,
  resolveBaseCommand,
  templateDefaultValues,
} from '../utils/buildCommand';
import { cloneName } from '../utils/format';

interface EnvEntry {
  key: string;
  value: string;
}

interface LabelEntry {
  key: string;
  value: string;
}

interface SubmitFormValues {
  name: string;
  image: string;
  gpus: number;
  queue: string;
  priority: Priority;
  command: string;
  env: EnvEntry[];
  labels: LabelEntry[];
  max_retries: number;
  // 表单清空时 InputNumber 会给出 null，提交前回退到 30
  termination_grace_period_seconds: number | null;
  // v0.9.1：训练参数由当前镜像的模板决定（契约 3/6），这些字段名不直接发给后端，
  // 而是由 buildCommandFromTemplate() 拼进命令行。
  params?: Record<string, unknown>;
  // 仅 /config 返回 max_nodes > 1 时展示
  multi_node?: boolean;
  nodes?: number | null;
  gpus_per_node?: number | null;
}

// 契约 1.6：labels 的键与值都须匹配该正则，总长 <= 256 字节
export const LABEL_PATTERN = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
export const MAX_LABELS_BYTES = 256;

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

// Form.List 的键值行：key/value 在新建行时都是空串
interface KeyValueRow {
  key?: string;
  value?: string;
}

// 把键值行收敛成对象；遇到重复键时返回该键，交由调用方提示
function rowsToRecord(rows: KeyValueRow[] | undefined): Record<string, string> | string {
  const record: Record<string, string> = {};
  for (const row of rows || []) {
    const key = (row.key ?? '').trim();
    if (!key) continue;
    if (Object.prototype.hasOwnProperty.call(record, key)) return key;
    record[key] = row.value ?? '';
  }
  return record;
}

// 仅用于 byte 计数提示：重复/空行忽略，不参与提交校验
function rowsToLabels(rows: KeyValueRow[] | undefined): Record<string, string> {
  const result = rowsToRecord(rows);
  return typeof result === 'string' ? {} : result;
}

function labelsByteLength(labels: Record<string, string>): number {
  return Object.entries(labels).reduce((sum, [key, value]) => sum + utf8Length(key) + utf8Length(value), 0);
}

/**
 * 表单里「训练命令」框此时应视为空：
 * - 用户完全没填 → 用模板 entrypoint
 * - 用户填的内容恰好等于 /config 的 default_command 且镜像提供了 entrypoint
 *   → 仍然用 entrypoint（说明用户没有真正手写命令，别让默认值挡住镜像模板）
 * 其余情况都算用户手填，优先级最高。
 */
export function pickBaseCommand(options: {
  formCommand?: string | null;
  template?: ParamsTemplate | null;
  defaultCommand?: string | null;
}): string {
  const formCommand = (options.formCommand ?? '').trim();
  const entrypoint = (options.template?.entrypoint ?? '').trim();
  const untouched = !formCommand || Boolean(entrypoint) && formCommand === (options.defaultCommand ?? '').trim();
  return resolveBaseCommand({
    userCommand: untouched ? '' : formCommand,
    entrypoint,
    defaultCommand: options.defaultCommand,
  });
}

export function SubmitJobPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const { user } = useAuth();
  const sourceJob = (location.state as { sourceJob?: Job } | null)?.sourceJob;
  const [config, setConfig] = useState<Awaited<ReturnType<typeof api.getConfig>> | null>(null);
  // 镜像候选：优先用 /images 的返回，加载失败时退化成只有默认镜像，不阻塞提交
  const [imageOptions, setImageOptions] = useState<string[]>([]);
  const [quota, setQuota] = useState<EffectiveQuota | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [template, setTemplate] = useState<'smoke' | 'custom'>(sourceJob ? 'custom' : 'smoke');
  // v0.9.1：当前镜像的参数模板（契约 4/6）
  const [paramsTemplate, setParamsTemplate] = useState<ParamsTemplate | null>(null);
  const [paramsSource, setParamsSource] = useState<ImageParamsResponse['source'] | null>(null);
  const [paramsWarning, setParamsWarning] = useState<string | undefined>(undefined);
  const [paramsLoading, setParamsLoading] = useState(false);
  const paramsRequestId = useRef(0);
  const [form] = Form.useForm<SubmitFormValues>();
  const multiNode = Form.useWatch('multi_node', form) ?? false;
  // 实时预览最终命令：训练参数会被拼接到 base command 后面
  const watchCommand = Form.useWatch('command', form);
  const watchImage = Form.useWatch('image', form);
  const watchParams = Form.useWatch(PARAMS_FIELD, form);
  // 外层 Form.Item 不会因嵌套字段变化而重渲染（FormContext 每次 setFields 都会换新），
  // 用整个表单快照兜底，保证预览随任意训练参数输入即时刷新。
  const formSnapshot = Form.useWatch([], form) as SubmitFormValues | undefined;
  const isAdmin = user?.role === 'admin';

  const baseCommand = pickBaseCommand({ formCommand: watchCommand, template: paramsTemplate, defaultCommand: config?.default_command });
  const commandPreview = useMemo(() => {
    const values = (watchParams ?? formSnapshot?.params ?? {}) as Record<string, unknown>;
    if (!paramsTemplate) return baseCommand;
    return buildCommandFromTemplate(paramsTemplate, values, baseCommand);
  }, [paramsTemplate, watchParams, formSnapshot?.params, baseCommand]);
  // 配额提示需要在多机/单机两种模式下都跟随表单当前选择
  const allValues = Form.useWatch([], form) ?? {};
  const nodes = Form.useWatch('nodes', form) ?? 1;
  const gpusPerNode = Form.useWatch('gpus_per_node', form) ?? 1;
  const labelRows = Form.useWatch('labels', form) ?? [];

  const maxNodes = config?.max_nodes ?? 1;
  const multiNodeEnabled = maxNodes > 1;
  const ownerName = user?.username || sourceJob?.owner || '';

  const loadConfig = async () => {
    setConfigLoading(true);
    try {
      const data = await api.getConfig();
      setConfig(data);
      setConfigError(null);
    } catch (reason) {
      if (!isUnauthorized(reason)) setConfigError(getErrorMessage(reason));
    } finally {
      setConfigLoading(false);
    }
  };

  // 配额只用于提示，取不到时（匿名/旧后端）静默忽略
  useEffect(() => {
    const controller = new AbortController();
    loadEffectiveQuota(controller.signal).then((value) => setQuota(value));
    return () => controller.abort();
  }, []);

  usePolling((signal) => {
    void api.getConfig(signal)
      .then((data) => {
        setConfig(data);
        setConfigError(null);
      })
      .catch((reason) => {
        if (!isUnauthorized(reason)) setConfigError(getErrorMessage(reason));
      })
      .finally(() => setConfigLoading(false));
  }, 60000);

  const requestedGpus = useMemo(() => {
    if (multiNodeEnabled && multiNode) {
      return Math.max(1, Number(nodes) || 1) * Math.max(1, Number(gpusPerNode) || 1);
    }
    return allValues.gpus ?? null;
  }, [multiNodeEnabled, multiNode, nodes, gpusPerNode, allValues]);

  // 镜像候选来自后端 /images：默认镜像 + 历史任务用过的镜像。
  // 失败时静默降级（仍可手输镜像名），避免因下拉数据缺失而无法提交。
  useEffect(() => {
    const controller = new AbortController();
    api
      .getImages(controller.signal)
      .then((data) => setImageOptions(data.items.map((item) => item.image)))
      .catch(() => setImageOptions([]));
    return () => controller.abort();
  }, []);

  /**
   * 镜像变化 → 拉模板 → 重置参数为模板默认值（契约 6）。
   *
   * 拉取失败（网络、404/503 或接口尚未上线）时回退到前端内置模板，不阻塞提交。
   * 用递增的 requestId 丢弃过期响应：镜像被快速切换时，先发的请求即便后到也不能覆盖。
   */
  useEffect(() => {
    const image = (watchImage ?? '').trim();
    const requestId = paramsRequestId.current + 1;
    paramsRequestId.current = requestId;

    const applyTemplate = (
      next: ParamsTemplate,
      source: ImageParamsResponse['source'],
      warning?: string,
    ) => {
      if (paramsRequestId.current !== requestId) return;
      setParamsTemplate(next);
      setParamsSource(source);
      setParamsWarning(warning);
      // 切换镜像时已填参数一律重置为模板默认值，避免 A 镜像的参数残留到 B 镜像
      form.setFieldValue(PARAMS_FIELD, templateDefaultValues(next));
    };

    if (!image) {
      // 还没填镜像：先用内置模板占位，保证表单结构和预览不抖
      applyTemplate(builtinTemplate(), 'builtin');
      return;
    }

    const controller = new AbortController();
    setParamsLoading(true);
    api
      .getImageParams(image, controller.signal)
      .then((response) => {
        if (response?.template && Array.isArray(response.template.params)) {
          applyTemplate(response.template, response.source, response.warning);
        } else {
          // 200 但响应体不符合契约：按失败处理
          applyTemplate(builtinTemplate(), 'error', '镜像参数模板格式无效，已回退到平台通用参数');
        }
      })
      .catch((reason) => {
        if (isAbortError(reason)) return;
        applyTemplate(builtinTemplate(), 'error', `读取镜像参数模板失败（${getErrorMessage(reason)}），已回退到平台通用参数`);
      })
      .finally(() => {
        if (paramsRequestId.current === requestId) setParamsLoading(false);
      });

    return () => controller.abort();
    // form 实例在整个页面生命周期内不变
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchImage]);

  const submit = async (values: SubmitFormValues) => {
    const env = rowsToRecord(values.env);
    if (typeof env === 'string') {
      message.error(`环境变量 ${env} 重复，请保留一项`);
      return;
    }
    const labels = rowsToRecord(values.labels);
    if (typeof labels === 'string') {
      message.error(`标签 ${labels} 重复，请保留一项`);
      return;
    }
    for (const [key, value] of Object.entries(labels)) {
      if (!LABEL_PATTERN.test(key) || !LABEL_PATTERN.test(value)) {
        message.error(`标签 ${key} 的键或值格式无效，仅允许小写字母、数字、中划线、下划线和点`);
        return;
      }
    }
    if (labelsByteLength(labels) > MAX_LABELS_BYTES) {
      message.error(`标签总长度超过 ${MAX_LABELS_BYTES} 字节，请精简后再提交`);
      return;
    }

    // 多机开启时 gpus = nodes * gpus_per_node（契约 1.7）；否则取表单选择的 GPU 数
    const gpus = multiNodeEnabled && values.multi_node
      ? Math.max(1, Number(values.nodes) || 1) * Math.max(1, Number(values.gpus_per_node) || 1)
      : values.gpus;
    // 基础命令优先级（契约 6）：用户手填 > 模板 entrypoint > /config 的 default_command
    const chosenBase = pickBaseCommand({ formCommand: values.command, template: paramsTemplate, defaultCommand: config?.default_command });
    const currentTemplate = paramsTemplate ?? builtinTemplate();
    const payload: SubmitJobRequest = {
      name: values.name.trim(),
      image: values.image.trim(),
      gpus,
      queue: values.queue,
      priority: values.priority,
      // 训练参数拼进命令行；不填任何参数时等价于原命令
      command: buildCommandFromTemplate(currentTemplate, (values.params ?? {}) as Record<string, unknown>, chosenBase),
      env,
      max_retries: values.max_retries,
      termination_grace_period_seconds: values.termination_grace_period_seconds ?? 30,
      labels,
    };
    if (multiNodeEnabled) {
      payload.nodes = multiNode ? Math.max(1, Number(values.nodes) || 1) : 1;
      if (multiNode) payload.gpus_per_node = Math.max(1, Number(values.gpus_per_node) || 1);
    }

    setSubmitting(true);
    try {
      const job = await api.submitJob(payload);
      message.success(`任务 ${job.name} 已提交`);
      navigate(`/jobs/${encodeURIComponent(job.name)}`, { replace: true });
    } catch (reason) {
      const text = getErrorMessage(reason);
      // 后端配额相关错误是 422，detail 形如 "GPU quota exceeded: 4 used of 4 allowed"
      if (isQuotaError(reason, text)) {
        message.error(text, 6);
        navigate('/jobs', { state: { quotaError: text } });
      } else {
        message.error(text);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const changeTemplate = (value: 'smoke' | 'custom') => {
    setTemplate(value);
  };

  const quotaHint = describeQuotaHint(quota, requestedGpus);
  const paramsCardTitle = paramsTemplate?.entrypoint ? '训练参数（镜像模板）' : '训练参数（可选）';

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>提交训练任务</Typography.Title>
          <Typography.Text type="secondary">通过平台配置 PyTorchJob，无需编写 YAML</Typography.Text>
        </div>
      </div>
      <ErrorAlert error={configError} onRetry={() => void loadConfig()} title="无法读取平台配置" />
      {sourceJob && (
        <Alert
          type="info"
          showIcon
          message={`已复制任务 ${sourceJob.name} 的参数`}
          description="请确认新的任务名称和资源参数，再提交创建任务。"
          className="clone-alert"
        />
      )}
      <Spin spinning={configLoading && !config}>
        {config && (
          <Form<SubmitFormValues>
            form={form}
            layout="vertical"
            onFinish={submit}
            requiredMark={false}
            initialValues={{
              name: sourceJob ? cloneName(sourceJob.name) : '',
              image: sourceJob?.image || config.default_image,
              gpus: sourceJob && config.allowed_gpus.includes(sourceJob.gpus) ? sourceJob.gpus : config.allowed_gpus[0],
              queue: config.default_queue,
              priority: sourceJob?.priority || config.priorities[1] || config.priorities[0],
              command: sourceJob?.command || config.default_command,
              env: sourceJob ? Object.entries(sourceJob.env).map(([key, value]) => ({ key, value })) : [{ key: 'PYTHONUNBUFFERED', value: '1' }],
              labels: sourceJob?.labels ? Object.entries(sourceJob.labels).map(([key, value]) => ({ key, value })) : [],
              max_retries: sourceJob?.max_retries ?? 0,
              termination_grace_period_seconds: sourceJob?.termination_grace_period_seconds ?? 30,
              multi_node: false,
              nodes: sourceJob?.nodes && sourceJob.nodes > 1 ? sourceJob.nodes : 2,
              gpus_per_node: config.allowed_gpus[0],
              params: {},
            }}
          >
            <Card title="任务模板" className="section-card">
              <Form.Item noStyle shouldUpdate={false}>
                <Radio.Group value={template} onChange={(event) => changeTemplate(event.target.value)}>
                  <Radio.Button value="smoke">GPU Smoke Test</Radio.Button>
                  <Radio.Button value="custom">VLA Custom Job</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Typography.Paragraph type="secondary" className="template-help">
                {template === 'smoke' ? '使用平台默认镜像和命令，验证 CUDA 与小型 PyTorch 训练。' : '填写自有训练镜像与命令，数据集和 checkpoint 使用固定挂载目录。'}
              </Typography.Paragraph>
            </Card>

            <Card title="任务归属" className="section-card">
              <Descriptions column={{ xs: 1, md: 2 }} size="small">
                <Descriptions.Item label="提交者（owner）" span={2}>
                  <Typography.Text strong>{ownerName || '—'}</Typography.Text>
                  <Typography.Text type="secondary" className="owner-hint">
                    由服务端按当前登录账号填写，不可修改；非管理员只能查看与取消自己的任务。
                  </Typography.Text>
                </Descriptions.Item>
              </Descriptions>
            </Card>

            <Card title="基本参数" className="section-card">
              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="name"
                    label="任务名称"
                    rules={[
                      { required: true, message: '请输入任务名称' },
                      { max: 50, message: '任务名称最多 50 个字符' },
                      { pattern: /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/, message: '仅允许小写字母、数字和中划线，且首尾不能是中划线' },
                    ]}
                  >
                    <Input placeholder="例如 exp001" maxLength={50} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  {/* 既可从下拉里选，也可直接敲镜像名：镜像可能已导入节点但平台还没用过。
                      AntD 的 Select 在 form 里保存的是字符串（不是数组），提交逻辑无需改动。 */}
                  <Form.Item name="image" label="训练镜像" rules={[{ required: true, whitespace: true, message: '请选择或输入训练镜像' }]}>
                    <AutoComplete
                      placeholder="选择已有镜像，或直接输入镜像名"
                      filterOption={(input, option) =>
                        String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())
                      }
                      options={imageOptions.map((image) => ({ value: image }))}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name="gpus" label="GPU 数量" rules={[{ required: true, message: '请选择 GPU 数量' }]}>
                    <Select
                      disabled={multiNodeEnabled && multiNode}
                      options={config.allowed_gpus.map((value) => ({ value, label: `${value} 卡` }))}
                      placeholder="请选择 GPU 数量"
                    />
                  </Form.Item>
                  <Typography.Text type="secondary" className="field-hint" data-testid="quota-hint">
                    {multiNodeEnabled && multiNode
                      ? '多机模式下由「节点数 × 每节点 GPU」计算，此选择不生效'
                      : quotaHint || '可选值由后端平台配置决定'}
                  </Typography.Text>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name="priority" label="优先级" rules={[{ required: true }]}>
                    <Select options={config.priorities.map((value) => ({ value, label: { low: '低', normal: '普通', high: '高' }[value] }))} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name="queue" label="Kueue 队列" rules={[{ required: true }]}>
                    <Select options={[{ value: config.default_queue, label: config.default_queue }]} />
                  </Form.Item>
                </Col>
              </Row>
              {multiNodeEnabled && (
                <div className="multi-node-block" data-testid="multi-node-options">
                  <Form.Item name="multi_node" label="多机训练" valuePropName="checked" extra={`平台已启用多机（最多 ${maxNodes} 个节点）`}>
                    <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                  </Form.Item>
                  {multiNode && (
                    <Row gutter={16}>
                      <Col xs={24} md={8}>
                        <Form.Item
                          name="nodes"
                          label="节点数"
                          rules={[
                            {
                              validator: (_, value: number | null | undefined) => {
                                const count = Number(value);
                                if (!Number.isInteger(count) || count < 2 || count > maxNodes) {
                                  return Promise.reject(new Error(`节点数须为 2 到 ${maxNodes} 之间的整数`));
                                }
                                return Promise.resolve();
                              },
                            },
                          ]}
                        >
                          <InputNumber min={2} max={maxNodes} precision={0} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={8}>
                        <Form.Item name="gpus_per_node" label="每节点 GPU" rules={[{ required: true, message: '请选择每节点 GPU 数量' }]}>
                          <Select options={config.allowed_gpus.map((value) => ({ value, label: `${value} 卡` }))} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={8}>
                        <Form.Item label="合计 GPU">
                          <Typography.Text strong data-testid="total-gpus">{`${requestedGpus ?? 0} 卡`}</Typography.Text>
                        </Form.Item>
                      </Col>
                    </Row>
                  )}
                  {multiNode && (
                    <Alert
                      type="warning"
                      showIcon
                      className="section-card"
                      message="多机训练依赖集群已配置 Kueue topology 与 torchrun rendezvous"
                      description="若集群仍只支持单机，提交将返回 422 multi-node training is not enabled。"
                    />
                  )}
                </div>
              )}
              <Form.Item
                name="command"
                label="训练命令"
                rules={[{ required: true, whitespace: true, message: '请输入训练命令' }]}
                extra={
                  paramsTemplate?.entrypoint
                    ? `命令由容器内 /bin/bash -lc 执行；留空（如清空后仅剩空格）则使用镜像模板的 entrypoint：${paramsTemplate.entrypoint}`
                    : '命令由容器内 /bin/bash -lc 执行'
                }
              >
                <Input.TextArea
                  rows={3}
                  placeholder={paramsTemplate?.entrypoint ? `留空则使用 ${paramsTemplate.entrypoint}` : 'python /app/train.py --steps 100'}
                />
              </Form.Item>
              <Form.Item name="max_retries" label="失败重启次数" rules={[{ required: true, message: '请设置失败重启次数' }]} extra="0 表示不重启，最多允许 2 次">
                <InputNumber min={0} max={2} precision={0} />
              </Form.Item>
              <Form.Item
                name="termination_grace_period_seconds"
                label="取消宽限期（秒）"
                extra="取消任务时留给训练进程退出的秒数。越小取消越快释放 GPU，越大越不容易丢掉未保存的 checkpoint"
              >
                <InputNumber min={1} max={600} precision={0} />
              </Form.Item>
            </Card>

            <Card title={paramsCardTitle} className="section-card">
              <TrainParamsForm
                template={paramsTemplate}
                source={paramsSource}
                warning={paramsWarning}
                loading={paramsLoading && !paramsTemplate}
                isAdmin={isAdmin}
              />
              <Typography.Text type="secondary" className="field-hint">
                最终命令预览：
                <Typography.Text code data-testid="command-preview">
                  {commandPreview || '（填写命令后显示）'}
                </Typography.Text>
              </Typography.Text>
            </Card>

            <Card title="环境变量" className="section-card">
              <Form.List name="env">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...rest }) => (
                      <Space key={key} align="baseline" className="env-row">
                        <Form.Item
                          {...rest}
                          name={[name, 'key']}
                          rules={[
                            { required: true, message: '请输入变量名' },
                            { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '变量名格式无效' },
                          ]}
                        >
                          <Input placeholder="变量名，例如 CUDA_VISIBLE_DEVICES" className="env-key" />
                        </Form.Item>
                        <Form.Item {...rest} name={[name, 'value']} rules={[{ required: true, message: '请输入变量值' }]}>
                          <Input placeholder="变量值" className="env-value" />
                        </Form.Item>
                        <MinusCircleOutlined onClick={() => remove(name)} aria-label="删除环境变量" />
                      </Space>
                    ))}
                    <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ key: '', value: '' })}>添加环境变量</Button>
                  </>
                )}
              </Form.List>
            </Card>

            <Card title="任务标签" className="section-card">
              <Typography.Paragraph type="secondary" className="template-help">
                可选，键值均为小写字母、数字、中划线或点，例如 team=perception。标签会写入 PyTorchJob 注解，不参与调度。
              </Typography.Paragraph>
              <Form.List name="labels">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...rest }) => (
                      <Space key={key} align="baseline" className="env-row">
                        <Form.Item
                          {...rest}
                          name={[name, 'key']}
                          rules={[
                            { required: true, message: '请输入标签键' },
                            { pattern: LABEL_PATTERN, message: '标签键格式无效' },
                          ]}
                        >
                          <Input placeholder="标签键，例如 team" className="env-key" aria-label="标签键" />
                        </Form.Item>
                        <Form.Item
                          {...rest}
                          name={[name, 'value']}
                          rules={[
                            { required: true, message: '请输入标签值' },
                            { pattern: LABEL_PATTERN, message: '标签值格式无效' },
                          ]}
                        >
                          <Input placeholder="标签值，例如 perception" className="env-value" aria-label="标签值" />
                        </Form.Item>
                        <MinusCircleOutlined onClick={() => remove(name)} aria-label="删除标签" />
                      </Space>
                    ))}
                    <Space direction="vertical" size={4}>
                      <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ key: '', value: '' })}>添加标签</Button>
                      <Typography.Text type="secondary">
                        {`已用 ${labelsByteLength(rowsToLabels(labelRows))} / ${MAX_LABELS_BYTES} 字节`}
                      </Typography.Text>
                    </Space>
                  </>
                )}
              </Form.List>
            </Card>

            <Card title="固定存储路径" className="section-card">
              <Typography.Paragraph>数据集（只读）：<Typography.Text code>{config.dataset_path}</Typography.Text></Typography.Paragraph>
              <Typography.Paragraph>Checkpoint（可写）：<Typography.Text code>{config.checkpoint_root}/&lt;任务名称&gt;</Typography.Text></Typography.Paragraph>
              <Typography.Text type="secondary">平台不允许通过网页挂载其他宿主机路径。</Typography.Text>
            </Card>

            <Button type="primary" htmlType="submit" size="large" icon={<SendOutlined />} loading={submitting} disabled={!config.allowed_gpus.length}>
              提交训练任务
            </Button>
          </Form>
        )}
      </Spin>
    </div>
  );
}

// 422 的配额错误：后端 detail 形如 "GPU quota exceeded: 4 used of 4 allowed"
export function isQuotaError(error: unknown, messageText?: string): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  if (status !== 422) return false;
  const text = messageText ?? getErrorMessage(error);
  return /quota exceeded/i.test(text);
}

/**
 * 配额剩余量提示：优先用 /auth/me 返回的实时用量（gpu_in_use / active_jobs）算剩余；
 * 服务端只给上限不给用量时退化为只显示上限，不臆造剩余。
 */
export function describeQuotaHint(quota: EffectiveQuota | null, requestedGpus: number | null): string {
  if (!quota) return '';
  const parts: string[] = [];
  if (quota.max_gpus !== null && quota.max_gpus !== undefined) {
    if (typeof quota.gpu_in_use === 'number') {
      const remaining = Math.max(0, quota.max_gpus - quota.gpu_in_use);
      parts.push(`GPU 配额：已用 ${quota.gpu_in_use} / ${quota.max_gpus} 卡，剩余 ${remaining} 卡`);
    } else {
      parts.push(`GPU 配额上限 ${quota.max_gpus} 卡`);
    }
  }
  if (quota.max_active_jobs !== null && quota.max_active_jobs !== undefined) {
    if (typeof quota.active_jobs === 'number') {
      const remaining = Math.max(0, quota.max_active_jobs - quota.active_jobs);
      parts.push(`活跃任务：${quota.active_jobs} / ${quota.max_active_jobs}，剩余 ${remaining} 个`);
    } else {
      parts.push(`活跃任务上限 ${quota.max_active_jobs} 个`);
    }
  }
  if (!parts.length) return '';
  const exceeded = requestedGpus !== null
    && typeof quota.gpu_in_use === 'number'
    && quota.max_gpus !== null
    && quota.max_gpus !== undefined
    && quota.gpu_in_use + requestedGpus > quota.max_gpus;
  return `${parts.join('；')}${exceeded ? '；本次申请的 GPU 数将超出配额' : ''}`;
}