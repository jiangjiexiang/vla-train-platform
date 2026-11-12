import {
  App,
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Radio,
  Spin,
  Typography,
} from 'antd';
import { SendOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, getErrorMessage, isUnauthorized } from '../api/client';
import { loadEffectiveQuota } from '../api/quota';
import { ErrorAlert } from '../components/ErrorAlert';
import { PARAMS_FIELD } from '../components/TrainParamsForm';
import { useAuth } from '../context/AuthContext';
import { usePolling } from '../hooks/usePolling';
import type { EffectiveQuota, Job } from '../types/api';
import { buildCommandFromTemplate } from '../utils/buildCommand';
import { cloneName } from '../utils/format';
import { BasicParamsSection, KeyValueSections, TrainingParamsSection } from './submitJob/SubmitJobSections';
import { useImageParams } from './submitJob/useImageParams';
import {
  buildSubmitJobPayload,
  describeQuotaHint,
  isQuotaError,
  pickBaseCommand,
  rowsToRecord,
  validateLabels,
} from './submitJob/utils';
import type { SubmitFormValues } from './submitJob/utils';

export {
  LABEL_PATTERN,
  MAX_LABELS_BYTES,
  describeQuotaHint,
  isQuotaError,
  pickBaseCommand,
  validateLabels,
} from './submitJob/utils';

export function SubmitJobPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const { user } = useAuth();
  const navState = location.state as { sourceJob?: Job; resumeFrom?: string } | null;
  const sourceJob = navState?.sourceJob;
  const resumeFrom = navState?.resumeFrom;
  const [config, setConfig] = useState<Awaited<ReturnType<typeof api.getConfig>> | null>(null);
  const [imageOptions, setImageOptions] = useState<string[]>([]);
  const [quota, setQuota] = useState<EffectiveQuota | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [template, setTemplate] = useState<'smoke' | 'custom'>(sourceJob ? 'custom' : 'smoke');
  const [form] = Form.useForm<SubmitFormValues>();

  const resumeApplied = useRef(false);
  useEffect(() => {
    if (!resumeFrom || resumeApplied.current) return;
    resumeApplied.current = true;
    void form.setFieldValue(['params', 'resume'], true);
    void form.setFieldValue(['params', 'resume_from'], resumeFrom);
  }, [resumeFrom, form]);

  const multiNode = Form.useWatch('multi_node', form) ?? false;
  const watchCommand = Form.useWatch('command', form);
  const watchImage = Form.useWatch('image', form);
  const watchParams = Form.useWatch(PARAMS_FIELD, form);
  const formValues = Form.useWatch([], form) as SubmitFormValues | undefined;
  const nodes = Form.useWatch('nodes', form) ?? 1;
  const gpusPerNode = Form.useWatch('gpus_per_node', form) ?? 1;
  const { template: paramsTemplate, source: paramsSource, warning: paramsWarning, loading: paramsLoading } = useImageParams(watchImage, form);

  const isAdmin = user?.role === 'admin';
  const maxNodes = config?.max_nodes ?? 1;
  const multiNodeEnabled = maxNodes > 1;
  const ownerName = user?.username || sourceJob?.owner || '';

  const baseCommand = pickBaseCommand({
    formCommand: watchCommand,
    template: paramsTemplate,
    defaultCommand: config?.default_command,
  });
  const commandPreview = useMemo(() => {
    const values = (watchParams ?? formValues?.params ?? {}) as Record<string, unknown>;
    if (!paramsTemplate) return baseCommand;
    return buildCommandFromTemplate(paramsTemplate, values, baseCommand);
  }, [paramsTemplate, watchParams, formValues?.params, baseCommand]);

  const requestedGpus = useMemo(() => {
    if (multiNodeEnabled && multiNode) {
      return Math.max(1, Number(nodes) || 1) * Math.max(1, Number(gpusPerNode) || 1);
    }
    return formValues?.gpus ?? null;
  }, [multiNodeEnabled, multiNode, nodes, gpusPerNode, formValues?.gpus]);

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

  useEffect(() => {
    const controller = new AbortController();
    api
      .getImages(controller.signal)
      .then((data) => setImageOptions(data.items.map((item) => item.image)))
      .catch(() => setImageOptions([]));
    return () => controller.abort();
  }, []);

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
    const labelError = validateLabels(labels);
    if (labelError) {
      message.error(labelError);
      return;
    }

    const payload = buildSubmitJobPayload(values, {
      paramsTemplate,
      defaultCommand: config?.default_command,
      multiNodeEnabled,
    });

    setSubmitting(true);
    try {
      const job = await api.submitJob(payload);
      message.success(`任务 ${job.name} 已提交`);
      navigate(`/jobs/${encodeURIComponent(job.name)}`, { replace: true });
    } catch (reason) {
      const text = getErrorMessage(reason);
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

  const quotaHint = describeQuotaHint(quota, requestedGpus);

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
                <Radio.Group value={template} onChange={(event) => setTemplate(event.target.value)}>
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

            <BasicParamsSection
              config={config}
              imageOptions={imageOptions}
              multiNode={multiNode}
              requestedGpus={requestedGpus}
              quotaHint={quotaHint}
              paramsTemplate={paramsTemplate}
            />
            <TrainingParamsSection
              template={paramsTemplate}
              source={paramsSource}
              warning={paramsWarning}
              loading={paramsLoading}
              isAdmin={isAdmin}
              commandPreview={commandPreview}
            />
            <KeyValueSections />

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
