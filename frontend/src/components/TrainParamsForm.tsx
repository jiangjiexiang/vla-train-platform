import { Alert, Col, Form, Input, InputNumber, Row, Select, Space, Switch, Tag, Typography } from 'antd';
import type { FormInstance } from 'antd';
import { useMemo } from 'react';

import type { ImageParamsResponse, ParamSpec, ParamsTemplate } from '../types/api';
import { isExtraParam } from '../utils/buildCommand';

/** 模板来源的中文提示（契约 6）。 */
export const SOURCE_HINTS: Record<ImageParamsResponse['source'], string> = {
  file: '使用镜像登记的参数模板',
  builtin: '该镜像未登记参数模板，使用平台通用参数',
  error: '读取镜像参数模板失败，已回退到平台通用参数',
};

/** 表单里训练参数使用的字段名前缀，避免与镜像/GPU 等字段冲突。 */
export const PARAMS_FIELD = 'params';

/** 单个参数在表单里的值。 */
export type ParamFieldValue = string | number | boolean | null;

export type ParamFieldValues = Record<string, ParamFieldValue>;

interface TrainParamsFormProps {
  /** 当前模板；为 null 表示还没拉到 */
  template: ParamsTemplate | null;
  /** 模板来源与警告文案 */
  source?: ImageParamsResponse['source'] | null;
  warning?: string;
  /** 提供给 Form.Item name 的前缀路径，默认 ['params'] */
  namePath?: (string | number)[];
  /** 拉取中 */
  loading?: boolean;
  /** 是否管理员（用于「去登记」说明） */
  isAdmin?: boolean;
  form?: FormInstance;
}

/** 内置模板的透传参数不渲染控件（它由下面统一的「其他参数」输入框承载）。 */
export function visibleParams(template: ParamsTemplate | null): ParamSpec[] {
  if (!template) return [];
  return (template.params ?? []).filter((param) => !isExtraParam(param));
}

/**
 * 按模板渲染训练参数控件（契约 6）：
 *   int / float → InputNumber（float 用 stringMode 保留 `1e-4` 字面形式）
 *   str / path  → Input
 *   bool        → Switch
 *   enum        → Select
 */
function ParamControl({ param }: { param: ParamSpec }) {
  const placeholder = param.placeholder ?? undefined;
  const width = '100%';
  switch (param.type) {
    case 'int':
      return (
        <InputNumber
          style={{ width }}
          placeholder={placeholder}
          min={param.min}
          max={param.max}
          precision={0}
          step={1}
        />
      );
    case 'float':
      // 不设置 precision：字符串原样保存，`1e-4` 不会被格式化成 `0.0001`
      return (
        <InputNumber<string>
          style={{ width }}
          placeholder={placeholder}
          min={param.min === undefined ? undefined : String(param.min)}
          max={param.max === undefined ? undefined : String(param.max)}
          stringMode
        />
      );
    case 'bool':
      return <Switch checkedChildren="开启" unCheckedChildren="关闭" />;
    case 'enum':
      return (
        <Select
          allowClear
          placeholder={placeholder ?? '默认'}
          options={(param.options ?? []).map((option) => ({ value: option, label: option }))}
        />
      );
    case 'str':
    case 'path':
    default:
      return <Input placeholder={placeholder} />;
  }
}

/** 单字段校验：required + min/max（InputNumber 已拦一部分，这里兜底字符串输入）。 */
function paramRules(param: ParamSpec) {
  const rules: Record<string, unknown>[] = [];
  if (param.type === 'bool') return rules;
  rules.push({
    required: Boolean(param.required),
    message: `${param.label} 为必填项`,
  });
  if (param.type === 'int' || param.type === 'float') {
    rules.push({
      validator: (_: unknown, value: ParamFieldValue) => {
        if (value === null || value === undefined || value === '') return Promise.resolve();
        const number = Number(value);
        if (Number.isNaN(number)) return Promise.reject(new Error(`${param.label} 必须是数字`));
        if (param.type === 'int' && !Number.isInteger(number)) {
          return Promise.reject(new Error(`${param.label} 必须是整数`));
        }
        if (param.min !== undefined && number < param.min) {
          return Promise.reject(new Error(`${param.label} 不能小于 ${param.min}`));
        }
        if (param.max !== undefined && number > param.max) {
          return Promise.reject(new Error(`${param.label} 不能大于 ${param.max}`));
        }
        return Promise.resolve();
      },
    });
  }
  return rules;
}

export function TrainParamsForm({
  template,
  source = null,
  warning,
  namePath = [PARAMS_FIELD],
  loading = false,
  isAdmin = false,
}: TrainParamsFormProps) {
  const params = useMemo(() => visibleParams(template), [template]);
  const extra = template?.extra_args;
  const extraEnabled = extra ? extra.enabled !== false : false;
  const sourceHint = source ? SOURCE_HINTS[source] : '';

  return (
    <div data-testid="train-params">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {sourceHint && (
          <Space size={8} wrap data-testid="params-source-hint">
            <Tag color={source === 'file' ? 'blue' : source === 'builtin' ? 'gold' : 'red'}>
              {source === 'file' ? '镜像模板' : source === 'builtin' ? '平台通用参数' : '回退通用参数'}
            </Tag>
            <Typography.Text type="secondary">{sourceHint}</Typography.Text>
            {source === 'builtin' && isAdmin && (
              <Typography.Text type="secondary" data-testid="params-register-hint">
                （管理员可在「镜像参数模板」中登记该镜像自己的模板）
              </Typography.Text>
            )}
          </Space>
        )}
        {source === 'error' && warning && (
          <Alert type="warning" showIcon message={warning} data-testid="params-warning" />
        )}
        {loading && (
          <Typography.Text type="secondary" data-testid="params-loading">
            正在读取镜像参数模板…
          </Typography.Text>
        )}

        {!loading && params.length === 0 && !extraEnabled && (
          <Typography.Text type="secondary" data-testid="params-empty">
            该镜像未声明任何训练参数，命令行将直接使用上面的基础命令。
          </Typography.Text>
        )}

        {params.length > 0 && (
          <Row gutter={16}>
            {params.map((param) => (
              <Col xs={24} md={param.type === 'bool' ? 12 : 8} key={param.key}>
                <Form.Item
                  name={[...namePath, param.key]}
                  label={param.label}
                  extra={param.help}
                  rules={paramRules(param)}
                  valuePropName={param.type === 'bool' ? 'checked' : 'value'}
                  data-testid={`param-${param.key}`}
                >
                  <ParamControl param={param} />
                </Form.Item>
              </Col>
            ))}
          </Row>
        )}

        {extraEnabled && (
          <Form.Item
            name={[...namePath, 'extra_args']}
            label={extra?.label ?? '其他参数'}
            extra={extra?.help ?? '原样追加到命令行末尾，不做引号包裹与解析'}
          >
            <Input placeholder="原样追加到命令末尾，例如 --freeze-vision-encoder" allowClear />
          </Form.Item>
        )}
      </Space>
    </div>
  );
}