import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Typography,
} from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';
import { TrainParamsForm } from '../../components/TrainParamsForm';
import type { ImageParamsResponse, ParamsTemplate, PlatformConfig } from '../../types/api';
import {
  LABEL_PATTERN,
  MAX_LABELS_BYTES,
  labelsByteLength,
  rowsToLabels,
} from './utils';
import type { KeyValueRow } from './utils';

export function BasicParamsSection({
  config,
  imageOptions,
  multiNode,
  requestedGpus,
  quotaHint,
  paramsTemplate,
}: {
  config: PlatformConfig;
  imageOptions: string[];
  multiNode: boolean;
  requestedGpus: number | null;
  quotaHint: string;
  paramsTemplate: ParamsTemplate | null;
}) {
  const maxNodes = config.max_nodes ?? 1;
  const multiNodeEnabled = maxNodes > 1;

  return (
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
  );
}

export function TrainingParamsSection({
  template,
  source,
  warning,
  loading,
  isAdmin,
  commandPreview,
}: {
  template: ParamsTemplate | null;
  source: ImageParamsResponse['source'] | null;
  warning: string | undefined;
  loading: boolean;
  isAdmin: boolean;
  commandPreview: string;
}) {
  return (
    <Card title={template?.entrypoint ? '训练参数（镜像模板）' : '训练参数（可选）'} className="section-card">
      <TrainParamsForm
        template={template}
        source={source}
        warning={warning}
        loading={loading && !template}
        isAdmin={isAdmin}
      />
      <Typography.Text type="secondary" className="field-hint">
        最终命令预览：
        <Typography.Text code data-testid="command-preview">
          {commandPreview || '（填写命令后显示）'}
        </Typography.Text>
      </Typography.Text>
    </Card>
  );
}

export function KeyValueSections() {
  const labelRows = (Form.useWatch('labels') ?? []) as KeyValueRow[];

  return (
    <>
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
    </>
  );
}
