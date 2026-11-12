import { Alert, Col, Form, Input, InputNumber, Row, Select, Switch, Typography } from 'antd';
import type { FormInstance } from 'antd';
import { useRef } from 'react';

import { PARAMS_FIELD, paramRules, visibleParams } from '../../components/TrainParamsForm';
import type { ParamSpec, Recipe } from '../../types/api';
import { paramPlaceholder } from '../../utils/dataset';
import { recipeTemplate } from './utils';
import { useEstimate } from './useEstimate';

/** 参数推荐值的影子字段后缀，只用于展示，不会出现在提交的 overrides 里。 */
const DEFAULT_VALUE_SUFFIX = '__default';

/**
 * 快速开始的参数控件：类型映射与 TrainParamsForm.ParamControl 保持一致
 * （int/float → InputNumber，bool → Switch，enum → Select，其余 → Input）。
 * 表单初值由 ParamsPanel 里的 Form.Item 负责，这里只补一个占位文案。
 */
function QuickParamControl({ param }: { param: ParamSpec }) {
  const placeholder = paramPlaceholder(param);
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

/** 把配方推荐值渲染成「默认 5」这样的提示，不改动控件本身。 */
function DefaultValueHint({ param }: { param: ParamSpec }) {
  const text = paramPlaceholder(param);
  if (param.type === 'bool' || !text) return null;
  return (
    <Typography.Text type="secondary" className="param-default-hint">
      默认 {text}
    </Typography.Text>
  );
}

/**
 * 高级参数面板。
 *
 * 不直接复用 TrainParamsForm：它内部的 Form.Item 用相对路径 (namePath=[]) 注册，
 * 而 antd 5 的 Form.Item 不会把父级 name 前缀拼进子字段，参数既进不了 store 也不会被校验。
 * 这里沿用同一套控件类型映射与校验规则，显式给出 [params, key] 的绝对路径。
 *
 * 默认值处理：Field 只在挂载那一刻读一次 store，而折叠面板展开时 Field 才真正渲染，
 * 之后写 store 不会回填到控件。因此在 ParamsPanel 渲染（早于 Field 挂载）时先把配方
 * defaults 写进 store；另外为每个参数准备一条 `params.<key>__default` 字段，专门用来
 * 把推荐值显示成输入框的占位文案——折叠状态下也能看出「不改会用什么值」。
 */
export function ParamsPanel({
  form,
  recipe,
  defaults,
}: {
  form: FormInstance;
  recipe: Recipe;
  defaults: Record<string, unknown>;
}) {
  // 渲染阶段写 store：此时的写入发生在 Field 挂载/订阅之前，控件挂载后直接读出，
  // 之后再改用户输入也不会被覆盖。
  const appliedRef = useRef<string | null>(null);
  if (appliedRef.current !== recipe.id) {
    appliedRef.current = recipe.id;
    form.setFields([
      ...Object.entries(defaults).map(([key, value]) => ({ name: [PARAMS_FIELD, key], value })),
      ...Object.entries(defaults).map(([key, value]) => ({
        name: [PARAMS_FIELD, `${key}${DEFAULT_VALUE_SUFFIX}`],
        value,
      })),
    ]);
  }

  const params = visibleParams(recipeTemplate(recipe));
  // 预估耗时：用户填 5000 步时最想知道的就是这个
  const estimateInfo = useEstimate(recipe?.id, recipe);
  return (
    <div data-testid="quickstart-params">
      {estimateInfo ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`预计训练耗时 ${estimateInfo.range}`}
          description={estimateInfo.hint}
          data-testid="quickstart-estimate"
        />
      ) : null}
      <Row gutter={16}>
        {params.map((param) => (
          <Col xs={24} md={param.type === 'bool' ? 12 : 8} key={param.key}>
            <Form.Item
              name={[PARAMS_FIELD, param.key]}
              label={param.label}
              extra={param.help}
              rules={paramRules(param)}
              valuePropName={param.type === 'bool' ? 'checked' : 'value'}
              data-testid={`param-${param.key}`}
            >
              <QuickParamControl param={param} />
            </Form.Item>
            {param.type !== 'bool' && (
              <Form.Item noStyle name={[PARAMS_FIELD, `${param.key}${DEFAULT_VALUE_SUFFIX}`]}>
                <DefaultValueHint param={param} />
              </Form.Item>
            )}
          </Col>
        ))}
      </Row>
    </div>
  );
}
