import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Steps,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CheckCircleFilled,
  DatabaseOutlined,
  ExclamationCircleOutlined,
  FireOutlined,
  ReloadOutlined,
  RocketOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { ApiError, api, getErrorMessage, isAbortError, isUnauthorized } from '../api/client';
import {
  DatasetStatusTag,
  DatasetWarningHint,
  isDatasetUsable,
  issuesByLevel,
} from '../components/DatasetBadges';
import { ErrorAlert } from '../components/ErrorAlert';
import { PARAMS_FIELD, paramRules, visibleParams } from '../components/TrainParamsForm';
import type { ParamSpec } from '../types/api';
import type { DatasetMeta, ParamsTemplate, Priority, Recipe, RunRequest } from '../types/api';
import { formatBytes, formatCount, formatMinutes, paramPlaceholder } from '../utils/dataset';

/**
 * 页面间传递「用此数据聚集开始训练」的预选。走 history state 而不是 query，
 * 刷新后不会残留一个可能已被删除的 dataset id。
 */
export interface QuickStartLocationState {
  datasetId?: string;
}

export const PRIORITY_OPTIONS: Array<{ value: Priority; label: string }> = [
  { value: 'low', label: '低' },
  { value: 'normal', label: '普通' },
  { value: 'high', label: '高' },
];

/** 配方能否吃下这个数据集：格式必须精确匹配（与后端 check_compatibility 一致）。 */
export function isRecipeCompatible(recipe: Recipe, dataset: DatasetMeta | null): boolean {
  if (!dataset) return false;
  return (recipe.dataset_formats ?? []).includes(dataset.format);
}

/** 把配方列表项转成 TrainParamsForm 需要的模板结构。 */
function recipeTemplate(recipe: Recipe): ParamsTemplate {
  return {
    schema_version: 1,
    entrypoint: recipe.entrypoint,
    params: recipe.params ?? [],
    extra_args: recipe.extra_args ?? undefined,
  };
}

/**
 * 参数初始值：模板 default 为主，配方的 defaults 覆盖模板（与后端
 * runs.merge_values 的优先级 defaults > default 一致）。bool 缺省显式给 false，
 * 保证 Switch 受控；enum 缺省时取第一个选项。
 */
export function recipeInitialParams(recipe: Recipe): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const param of visibleParams(recipeTemplate(recipe))) {
    if (param.type === 'bool') {
      values[param.key] = false;
    } else if (param.default !== undefined && param.default !== null && param.default !== '') {
      values[param.key] = param.default;
    } else if (param.type === 'enum' && param.options && param.options.length > 0) {
      values[param.key] = param.options[0];
    } else {
      values[param.key] = null;
    }
  }
  for (const [key, value] of Object.entries(recipe.defaults ?? {})) {
    if (key in values) values[key] = value;
  }
  return values;
}

/** 只把用户真正改过的参数放进 overrides，未改的交给后端用 defaults/模板默认值。 */
export function buildOverrides(
  recipe: Recipe | null,
  params: Record<string, unknown> | undefined,
): Record<string, unknown> {
  if (!recipe || !params) return {};
  const initial = recipeInitialParams(recipe);
  const overrides: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (key === 'extra_args') continue;
    if (value === null || value === undefined || value === '') continue;
    if (!(key in initial)) continue;
    if (value !== initial[key]) overrides[key] = value;
  }
  return overrides;
}

/**
 * 422 的 detail 分两种：数据集本身不可用，或配方不支持该格式。两者处理方式完全不同，
 * 提示要分开写，否则用户会去换配方而问题其实在数据上。
 */
export function describeSubmitError(error: unknown): { title: string; description: string } {
  const text = getErrorMessage(error);
  if (error instanceof ApiError && error.status === 409) {
    return {
      title: '任务名已被占用',
      description: `${text}。请换一个任务名，或清空任务名让平台自动生成。`,
    };
  }
  if (error instanceof ApiError && error.status === 422) {
    if (/数据集.*不可用|dataset.*unavailable/i.test(text)) {
      return {
        title: '数据集不可用',
        description: `${text}。请到「数据集」页查看问题并修复后重新扫描，或换一个可用数据集。`,
      };
    }
    if (/不支持数据集格式|不支持|格式/i.test(text)) {
      return {
        title: '数据集与配方不兼容',
        description: `${text}。请换一个支持该格式的配方，或先把数据集转换格式。`,
      };
    }
    return { title: '提交参数有误', description: text };
  }
  return { title: '提交失败', description: text };
}

const STEPS = [
  { title: '选数据集', description: '确认格式与规模' },
  { title: '选配方', description: '现成的训练方案' },
  { title: '加入队列', description: '平台自动补全参数' },
];

// 网格里的卡片用 role=radio 暴露可选中状态，比裸 div 更好测也更好读
function DatasetCard({
  dataset,
  selected,
  onSelect,
}: {
  dataset: DatasetMeta;
  selected: boolean;
  onSelect: () => void;
}) {
  const usable = isDatasetUsable(dataset);
  const errors = issuesByLevel(dataset.issues, 'error');
  const warnings = issuesByLevel(dataset.issues, 'warning');

  const metaItems = [
    { label: '集数', value: formatCount(dataset.episodes) },
    { label: '帧数', value: formatCount(dataset.frames) },
    { label: '时长', value: formatMinutes(dataset.duration_seconds) },
    { label: '相机', value: `${dataset.cameras?.length ?? 0} 路` },
    { label: '维度', value: `state ${dataset.state_dim} / action ${dataset.action_dim}` },
    { label: '体积', value: formatBytes(dataset.size_bytes) },
  ];

  const body = (
    <div
      className={`pick-card dataset-card${selected ? ' pick-card-selected' : ''}${usable ? '' : ' pick-card-disabled'}`}
      role="radio"
      aria-checked={selected}
      aria-disabled={!usable}
      tabIndex={usable ? 0 : -1}
      data-testid={`dataset-card-${dataset.id}`}
      onClick={() => {
        if (usable) onSelect();
      }}
      onKeyDown={(event) => {
        if (usable && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="pick-card-head">
        <Space size={6} wrap>
          <Typography.Text strong>{dataset.name}</Typography.Text>
          <DatasetStatusTag status={dataset.status} />
          <Tag bordered={false}>{dataset.format || 'unknown'}</Tag>
          <DatasetWarningHint issues={dataset.issues} />
        </Space>
        {selected && <CheckCircleFilled className="pick-card-check" />}
      </div>
      <div className="pick-card-meta">
        {metaItems.map((item) => (
          <div key={item.label} className="pick-meta-item">
            <span className="pick-meta-label">{item.label}</span>
            <span className="pick-meta-value">{item.value}</span>
          </div>
        ))}
      </div>
      {!usable && (
        <Typography.Text type="danger" className="pick-card-reason">
          <ExclamationCircleOutlined /> {errors[0]?.message || '数据集不可用，请先修复后重新扫描'}
        </Typography.Text>
      )}
      {usable && warnings.length > 0 && (
        <Typography.Text type="warning" className="pick-card-reason">
          {warnings.length} 条警告：{warnings[0].message}
        </Typography.Text>
      )}
    </div>
  );

  // 不可用卡片 hover 时把全部 error 列出来，不必先点到详情页
  return usable ? body : (
    <Tooltip title={(
      <div>
        {errors.map((issue, index) => <div key={`${issue.code}-${index}`}>· {issue.message}</div>)}
      </div>
    )}>
      {/* Tooltip 需要能捕获鼠标事件的包裹层，disabled 的卡片仍可 hover */}
      <div className="pick-card-tooltip-wrap">{body}</div>
    </Tooltip>
  );
}

function RecipeCard({
  recipe,
  selected,
  onSelect,
}: {
  recipe: Recipe;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <div
      className={`pick-card recipe-card${selected ? ' pick-card-selected' : ''}`}
      role="radio"
      aria-checked={selected}
      tabIndex={0}
      data-testid={`recipe-card-${recipe.id}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="pick-card-head">
        <Space size={6} wrap>
          <Typography.Text strong>{recipe.name}</Typography.Text>
          {recipe.estimated_vram_gb !== null && recipe.estimated_vram_gb !== undefined && (
            <Tag bordered={false} icon={<ThunderboltOutlined />}>{recipe.estimated_vram_gb} GB 显存</Tag>
          )}
          <Tag bordered={false}>{recipe.gpu_count} × GPU</Tag>
        </Space>
        {selected && <CheckCircleFilled className="pick-card-check" />}
      </div>
      <Typography.Paragraph type="secondary" className="pick-card-desc" ellipsis={{ rows: 2 }}>
        {recipe.description || '暂无描述'}
      </Typography.Paragraph>
    </div>
  );
}

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

/**
 * 高级参数面板。
 *
 * 不直接复用 TrainParamsForm：它内部的 Form.Item 用相对路径 (namePath=[]) 注册，
 * 而 antd 5 的 Form.Item 不会把父级 name 前缀拼进子字段，参数既进不了 store 也不会被校验。
 * 这里沿用同一套控件类型映射与校验规则，显式给出 [params, key] 的绝对路径。
 */
function ParamsPanel({ recipe }: { recipe: Recipe }) {
  const params = visibleParams(recipeTemplate(recipe));
  return (
    <div data-testid="quickstart-params">
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
          </Col>
        ))}
      </Row>
    </div>
  );
}

export function QuickStartPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = App.useApp();
  const preselectedId = (location.state as QuickStartLocationState | null)?.datasetId;

  const [datasets, setDatasets] = useState<DatasetMeta[]>([]);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [loadingDatasets, setLoadingDatasets] = useState(true);
  const [loadingRecipes, setLoadingRecipes] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [datasetError, setDatasetError] = useState<string | null>(null);
  const [recipeError, setRecipeError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [datasetId, setDatasetId] = useState<string | null>(preselectedId ?? null);
  const [recipeId, setRecipeId] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<{ title: string; description: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();

  const applyDatasets = (items: DatasetMeta[]) => {
    setDatasets(items);
    // 预选/已选的数据集可能已被删除（或变成不可用），失效时直接清掉选择
    setDatasetId((current) => {
      if (!current) return current;
      const found = items.find((item) => item.id === current);
      return found && isDatasetUsable(found) ? current : null;
    });
  };

  const loadDatasets = async (refresh = false) => {
    if (refresh) setRefreshing(true);
    else setLoadingDatasets(true);
    try {
      const data = await api.listDatasets(refresh);
      applyDatasets(data.items ?? []);
      setDatasetError(null);
    } catch (reason) {
      if (isAbortError(reason) || isUnauthorized(reason)) return;
      setDatasetError(getErrorMessage(reason));
    } finally {
      setRefreshing(false);
      setLoadingDatasets(false);
    }
  };

  useEffect(() => {
    void loadDatasets(false);
    const controller = new AbortController();
    api
      .listRecipes()
      .then((data) => {
        if (controller.signal.aborted) return;
        setRecipes(data.items ?? []);
        setRecipeError(null);
      })
      .catch((reason) => {
        if (isAbortError(reason) || isUnauthorized(reason)) return;
        setRecipeError(getErrorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingRecipes(false);
      });
    return () => controller.abort();
    // 只在首次进入时拉取；「重新扫描」按钮单独走 refresh
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filteredDatasets = useMemo(() => {
    const text = keyword.trim().toLowerCase();
    const list = text
      ? datasets.filter((item) =>
          item.name.toLowerCase().includes(text) || item.id.toLowerCase().includes(text))
      : datasets;
    // 可用的排前面，不可用的沉底，避免坏数据挡住视线
    return [...list].sort((a, b) => Number(isDatasetUsable(b)) - Number(isDatasetUsable(a)));
  }, [datasets, keyword]);

  const selectedDataset = useMemo(
    () => datasets.find((item) => item.id === datasetId) ?? null,
    [datasets, datasetId],
  );

  const compatibleRecipes = useMemo(
    () => recipes.filter((recipe) => isRecipeCompatible(recipe, selectedDataset)),
    [recipes, selectedDataset],
  );

  // 数据集一变，旧配方可能不再兼容：自动回落到第一个兼容配方
  useEffect(() => {
    if (compatibleRecipes.length === 0) {
      setRecipeId(null);
      return;
    }
    setRecipeId((current) =>
      current && compatibleRecipes.some((recipe) => recipe.id === current)
        ? current
        : compatibleRecipes[0].id);
  }, [compatibleRecipes]);

  const selectedRecipe = useMemo(
    () => compatibleRecipes.find((recipe) => recipe.id === recipeId) ?? null,
    [compatibleRecipes, recipeId],
  );

  // 换配方 → 参数面板按新模板重建（见 ParamsPanel 的 key），上一次的提交错误也不再适用
  const selectedRecipeId = selectedRecipe?.id ?? null;
  useEffect(() => {
    setSubmitError(null);
  }, [selectedRecipeId]);

  const currentStep = !selectedDataset ? 0 : !selectedRecipe ? 1 : 2;
  (window as unknown as { __form?: unknown }).__form = form;
  const canSubmit = Boolean(selectedDataset && selectedRecipe) && !submitting;

  const handleSubmit = async () => {
    if (!selectedDataset || !selectedRecipe) return;
    const values = form.getFieldsValue() as { name?: string; priority?: Priority; params?: Record<string, unknown> };
    const params = (values.params ?? {}) as Record<string, unknown>;
    const payload: RunRequest = {
      recipe_id: selectedRecipe.id,
      dataset_id: selectedDataset.id,
      overrides: buildOverrides(selectedRecipe, params),
      priority: values.priority ?? 'normal',
    };
    const name = (values.name ?? '').trim();
    if (name) payload.name = name;

    setSubmitError(null);
    setSubmitting(true);
    try {
      const job = await api.createRun(payload);
      message.success(`任务 ${job.name} 已加入训练队列`);
      navigate(`/jobs/${encodeURIComponent(job.name)}`);
    } catch (reason) {
      if (!isUnauthorized(reason)) setSubmitError(describeSubmitError(reason));
    } finally {
      setSubmitting(false);
    }
  };

  const datasetEmpty = !loadingDatasets && !datasetError && filteredDatasets.length === 0;
  const paramCount = selectedRecipe ? visibleParams(recipeTemplate(selectedRecipe)).length : 0;

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>快速开始训练</Typography.Title>
          <Typography.Text type="secondary">
            选数据集 → 选配方 → 点开始。镜像、命令、环境变量和显存配置由平台自动补齐。
          </Typography.Text>
        </div>
        <Button size="small" type="text" onClick={() => navigate('/advanced/submit')}>
          需要更多控制？使用高级提交
        </Button>
      </div>

      <Card className="section-card quickstart-steps" size="small">
        <Steps
          current={currentStep}
          items={STEPS.map((step) => ({ title: step.title, description: step.description }))}
        />
      </Card>

      {submitError && (
        <Alert
          type="error"
          showIcon
          closable
          onClose={() => setSubmitError(null)}
          message={submitError.title}
          description={submitError.description}
          data-testid="submit-error"
          className="error-alert"
        />
      )}

      {/* ---- 第一步：数据集 ---- */}
      <Card
        className="section-card"
        title={<Space><DatabaseOutlined /> 第一步 · 选择数据集</Space>}
        extra={(
          <Space size={8}>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder="搜索数据集名称"
              className="quickstart-search"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={() => void loadDatasets(true)}
            >
              重新扫描
            </Button>
          </Space>
        )}
      >
        <ErrorAlert error={datasetError} onRetry={() => void loadDatasets(true)} title="数据集加载失败" />
        <Spin spinning={loadingDatasets && datasets.length === 0}>
          {datasetEmpty ? (
            <Empty
              description={keyword.trim() ? `没有匹配「${keyword.trim()}」的数据集` : '还没有扫描到数据集'}
            >
              <Button icon={<ReloadOutlined />} onClick={() => void loadDatasets(true)}>重新扫描</Button>
            </Empty>
          ) : (
            <div role="radiogroup" aria-label="数据集" className="pick-grid">
              {filteredDatasets.map((dataset) => (
                <DatasetCard
                  key={dataset.id}
                  dataset={dataset}
                  selected={dataset.id === datasetId}
                  onSelect={() => setDatasetId(dataset.id)}
                />
              ))}
            </div>
          )}
        </Spin>
      </Card>

      {/* ---- 第二步：配方 ---- */}
      <Card
        className="section-card"
        title={<Space><FireOutlined /> 第二步 · 选择训练配方</Space>}
        extra={selectedDataset ? (
          <Typography.Text type="secondary">
            已选 {selectedDataset.name}（{selectedDataset.format}），{compatibleRecipes.length} 个配方兼容
          </Typography.Text>
        ) : null}
      >
        <ErrorAlert error={recipeError} onRetry={() => window.location.reload()} title="配方加载失败" />
        {!selectedDataset ? (
          <Empty description="先在上方选一个数据集，这里会列出兼容的训练配方" />
        ) : loadingRecipes ? (
          <Spin />
        ) : compatibleRecipes.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="没有兼容的配方"
            description={`当前没有配方支持 ${selectedDataset.format || 'unknown'} 格式，请换一个数据集，或联系管理员补充配方。`}
          />
        ) : (
          <div role="radiogroup" aria-label="训练配方" className="pick-grid pick-grid-recipes">
            {compatibleRecipes.map((recipe) => (
              <RecipeCard
                key={recipe.id}
                recipe={recipe}
                selected={recipe.id === recipeId}
                onSelect={() => setRecipeId(recipe.id)}
              />
            ))}
          </div>
        )}
      </Card>

      {/* ---- 第三步：加入队列 ---- */}
      <Card
        className="section-card"
        title={<Space><RocketOutlined /> 第三步 · 加入训练队列</Space>}
        extra={selectedRecipe ? <Tag color="blue">{selectedRecipe.name}</Tag> : null}
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          initialValues={{ name: '', priority: 'normal' as Priority, params: {} }}
        >
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="任务名"
                extra="小写字母、数字和中划线，最长 50 个字符"
                rules={[
                  {
                    pattern: /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/,
                    message: '只能包含小写字母、数字和中划线，且不能以中划线开头或结尾',
                  },
                  { max: 50, message: '任务名最长 50 个字符' },
                ]}
              >
                <Input placeholder="留空自动生成" maxLength={50} allowClear />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="priority" label="优先级">
                <Radio.Group optionType="button" options={PRIORITY_OPTIONS} />
              </Form.Item>
            </Col>
          </Row>

          {selectedRecipe && (
            <div data-testid="probe-native">
              <Form.Item name="probe_plain" label="P1" initialValue="hello"><Input /></Form.Item>
              <Form.Item name={['params', 'steps']} label="P2" initialValue={42}><InputNumber /></Form.Item>
              <Form.Item name={['params', 'batch_size']} label="P3"><InputNumber /></Form.Item>
            </div>
          )}
          <Collapse
            ghost
            className="quickstart-advanced"
            items={[
              {
                key: 'params',
                label: paramCount > 0
                  ? `高级参数（可选，共 ${paramCount} 项，默认使用配方推荐值）`
                  : '高级参数（该配方没有可调参数）',
                children: selectedRecipe ? (
                  <ParamsPanel key={selectedRecipe.id} recipe={selectedRecipe} />
                ) : (
                  <Typography.Text type="secondary">请先选择数据集和配方</Typography.Text>
                ),
              },
            ]}
          />

          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Button
              type="primary"
              size="large"
              block
              icon={<RocketOutlined />}
              loading={submitting}
              disabled={!canSubmit && !submitting}
              onClick={() => void handleSubmit()}
            >
              加入训练队列
            </Button>
            {!selectedDataset && (
              <Typography.Text type="secondary">请先选择数据集</Typography.Text>
            )}
          </Space>
        </Form>
      </Card>
    </div>
  );
}