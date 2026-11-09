import { App, Alert, Button, Card, Col, Form, Input, InputNumber, Radio, Row, Select, Space, Spin, Typography } from 'antd';
import { MinusCircleOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, getErrorMessage } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { usePolling } from '../hooks/usePolling';
import type { Job, Priority, SubmitJobRequest } from '../types/api';
import { cloneName } from '../utils/format';

interface EnvEntry {
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
  max_retries: number;
}

export function SubmitJobPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const sourceJob = (location.state as { sourceJob?: Job } | null)?.sourceJob;
  const [config, setConfig] = useState<Awaited<ReturnType<typeof api.getConfig>> | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [template, setTemplate] = useState<'smoke' | 'custom'>(sourceJob ? 'custom' : 'smoke');

  const loadConfig = async () => {
    setConfigLoading(true);
    try {
      const data = await api.getConfig();
      setConfig(data);
      setConfigError(null);
    } catch (reason) {
      setConfigError(getErrorMessage(reason));
    } finally {
      setConfigLoading(false);
    }
  };

  usePolling((signal) => {
    void api.getConfig(signal)
      .then((data) => {
        setConfig(data);
        setConfigError(null);
      })
      .catch((reason) => setConfigError(getErrorMessage(reason)))
      .finally(() => setConfigLoading(false));
  }, 60000);

  const submit = async (values: SubmitFormValues) => {
    const env: Record<string, string> = {};
    for (const entry of values.env || []) {
      if (Object.prototype.hasOwnProperty.call(env, entry.key)) {
        message.error(`环境变量 ${entry.key} 重复，请保留一项`);
        return;
      }
      env[entry.key] = entry.value;
    }
    const payload: SubmitJobRequest = {
      ...values,
      name: values.name.trim(),
      image: values.image.trim(),
      command: values.command.trim(),
      env,
    };
    setSubmitting(true);
    try {
      const job = await api.submitJob(payload);
      message.success(`任务 ${job.name} 已提交`);
      navigate(`/jobs/${encodeURIComponent(job.name)}`, { replace: true });
    } catch (reason) {
      message.error(getErrorMessage(reason));
    } finally {
      setSubmitting(false);
    }
  };

  const changeTemplate = (value: 'smoke' | 'custom') => {
    setTemplate(value);
  };

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>提交训练任务</Typography.Title>
          <Typography.Text type="secondary">通过平台配置单节点 PyTorchJob，无需编写 YAML</Typography.Text>
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
              max_retries: sourceJob?.max_retries ?? 0,
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
                  <Form.Item name="image" label="训练镜像" rules={[{ required: true, whitespace: true, message: '请输入训练镜像' }]}>
                    <Input placeholder="例如 vla-smoke:latest" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name="gpus" label="GPU 数量" rules={[{ required: true, message: '请选择 GPU 数量' }]} extra="可选值由后端平台配置决定">
                    <Select options={config.allowed_gpus.map((value) => ({ value, label: `${value} 卡` }))} placeholder="请选择 GPU 数量" />
                  </Form.Item>
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
              <Form.Item name="command" label="训练命令" rules={[{ required: true, whitespace: true, message: '请输入训练命令' }]} extra="命令由容器内 /bin/bash -lc 执行">
                <Input.TextArea rows={3} placeholder="python /app/train.py --steps 100" />
              </Form.Item>
              <Form.Item name="max_retries" label="失败重启次数" rules={[{ required: true, message: '请设置失败重启次数' }]} extra="0 表示不重启，最多允许 2 次">
                <InputNumber min={0} max={2} precision={0} />
              </Form.Item>
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
