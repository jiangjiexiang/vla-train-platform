import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Col,
  Modal,
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
import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormInstance } from 'antd';
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
import type {
  DatasetMeta,
  ParamsTemplate,
  PreflightCheck,
  PreflightResponse,
  Priority,
  Recipe,
  RunRequest,
} from '../types/api';
import { formatBytes, formatCount, formatMinutes, paramPlaceholder } from '../utils/dataset';

/**
 * 页面间传递「用此数据聚集开始训练」的预选。走 history state 而不是 query，
 * 刷新后不会残留一个可能已被删除的 dataset id。
 */
export interface QuickStartLocationState {
  datasetId?: string;
}

/** 参数推荐值的影子字段后缀，只用于展示，不会出现在提交的 overrides 里。 */
const DEFAULT_VALUE_SUFFIX = '__default';

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

/** 预检 error → 页面顶部 Alert 的标题与说明。 */
export function describePreflightErrors(errors: PreflightCheck[]): { title: string; description: string } {
  const codes = new Set(errors.map((check) => check.code));
  const title = codes.has('dataset_unusable') || codes.has('no_camera')
    ? '数据集不可用，先修好再训练'
    : codes.has('format_mismatch')
      ? '数据集与配方不兼容'
      : '预检未通过';
  const description = errors.map((check) => check.message).join('；') || '请按下方建议处理后重试';
  return { title, description };
}

const STEPS = [
  { title: '选数据集', description: '确认格式与规模' },
  { title: '选配方', description: '现成的训练方案' },
  { title: '加入队列', description: '平台自动补全参数' },
];

// 网格里的卡片用 role=radio 暴露可选中状态，比裸 div 更好测也更好读

/** 训练耗时预估。失败就返回 null —— 估不出来不该挡住用户提交。 */
function useEstimate(recipeId: string | undefined, recipe: Recipe | undefined) {
  const [value, setValue] = useState<{ range: string; hint: string } | null>(null);
  const defaults = (recipe?.defaults ?? {}) as Record<string, unknown>;
  const steps = defaults.steps;

  useEffect(() => {
    if (!recipeId) {
      setValue(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const result = await api.estimateRun({
            recipe_id: recipeId,
            // 预估只用配方参数；后端不强制 dataset，这里给空串即可
            dataset_id: '',
            overrides: steps === undefined ? undefined : { steps: Number(steps) },
          });
          if (cancelled) return;
          if (!result.estimated_seconds) {
            setValue(null);
            return;
          }
          setValue({
            range: result.range_text || result.estimated_text,
            hint:
              result.source === 'history'
                ? `按 ${result.samples} 个同类历史任务实测（约 ${(result as { steps_per_second?: number }).steps_per_second ?? '?'} 步/秒）`
                : '暂无同类历史数据，用的是保守估计',
          });
        } catch {
          if (!cancelled) setValue(null);
        }
      })();
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [recipeId, steps]);

  return value;
}

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
            <Tag bordered={false} icon={<ThunderboltOutlined />}>约 {recipe.estimated_vram_gb} GB 显存</Tag>
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
 *
 * 默认值处理：Field 只在挂载那一刻读一次 store，而折叠面板展开时 Field 才真正渲染，
 * 之后写 store 不会回填到控件。因此在 ParamsPanel 渲染（早于 Field 挂载）时先把配方
 * defaults 写进 store；另外为每个参数准备一条 `params.<key>__default` 字段，专门用来
 * 把推荐值显示成输入框的占位文案——折叠状态下也能看出「不改会用什么值」。
 */
function ParamsPanel({
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

/** 预检结果：按 level 分色，逐条给出 message 与可执行的 suggestion。 */
export function PreflightList({ checks }: { checks: PreflightCheck[] }) {
  if (checks.length === 0) return null;
  return (
    <ul className="preflight-list" data-testid="preflight-list">
      {checks.map((check, index) => (
        <li key={`${check.code}-${index}`} className={`dataset-issue dataset-issue-${check.level}`}>
          <div>
            <Typography.Text
              strong
              type={check.level === 'error' ? 'danger' : check.level === 'warning' ? 'warning' : undefined}
            >
              {check.message}
            </Typography.Text>
            <Typography.Text type="secondary" className="preflight-suggestion">
              {check.suggestion}
            </Typography.Text>
          </div>
        </li>
      ))}
    </ul>
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
  // 预检结果：error 直接拦住，warning/info 交由用户在弹窗里确认
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [confirming, setConfirming] = useState(false);
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
    setPreflight(null);
  }, [selectedRecipeId]);

  const currentStep = !selectedDataset ? 0 : !selectedRecipe ? 1 : 2;
  const canSubmit = Boolean(selectedDataset && selectedRecipe) && !submitting;

  /** 组装 /runs 与 /runs/preflight 共用的请求体。 */
  const buildPayload = (): RunRequest | null => {
    if (!selectedDataset || !selectedRecipe) return null;
    const values = form.getFieldsValue() as {
      name?: string;
      priority?: Priority;
      params?: Record<string, unknown>;
    };
    const payload: RunRequest = {
      recipe_id: selectedRecipe.id,
      dataset_id: selectedDataset.id,
      overrides: buildOverrides(selectedRecipe, values.params),
      priority: values.priority ?? 'normal',
    };
    const name = (values.name ?? '').trim();
    if (name) payload.name = name;
    return payload;
  };

  const submit = async (payload: RunRequest) => {
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

  /**
   * 「加入训练队列」：先预检，把问题挡在入队之前。
   *   - 有 error → 页面顶部展示（含 suggestion），不提交；
   *   - 只有 warning/info → 弹窗确认，确认后才真正提交；
   *   - 预检接口本身失败 → 退化为直接提交，由后端在 /runs 里再拦一次。
   */
  const handleSubmit = async () => {
    const payload = buildPayload();
    if (!payload) return;
    // 表单校验不通过时 input 自身已提示，这里只拦下提交
    const invalid = form.getFieldsError().some((field) => field.errors.length > 0);
    if (invalid) return;

    setSubmitError(null);
    setSubmitting(true);
    let result: PreflightResponse | null = null;
    try {
      result = await api.preflightRun(payload);
    } catch (reason) {
      if (isUnauthorized(reason)) return;
      // 预检不可用不阻塞训练：后端 create_run 内部同样会预检
      result = null;
    } finally {
      setSubmitting(false);
    }

    if (!result) {
      await submit(payload);
      return;
    }
    const errors = result.checks.filter((check) => check.level === 'error');
    if (!result.ok || errors.length > 0) {
      setPreflight(result);
      setSubmitError(describePreflightErrors(errors));
      return;
    }
    // 只有 warning 才需要用户确认。
    // info（显存预估、任务将排队）是纯信息提示，每次都弹窗会让用户形成「闭眼点确定」
    // 的习惯，反而忽略真正需要注意的 warning。
    const warnings = result.checks.filter((check) => check.level === 'warning');
    if (warnings.length > 0) {
      setPreflight(result);
      setConfirming(true);
      return;
    }
    await submit(payload);
  };

  const continueSubmit = async () => {
    setConfirming(false);
    const payload = buildPayload();
    if (payload) await submit(payload);
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
          description={(
            <div>
              <div>{submitError.description}</div>
              {preflight && <PreflightList checks={preflight.checks} />}
            </div>
          )}
          data-testid="submit-error"
          className="error-alert"
        />
      )}

      <Modal
        open={confirming}
        title="预检发现一些提醒，仍要加入队列吗？"
        okText="仍然提交"
        cancelText="返回修改"
        okButtonProps={{ loading: submitting }}
        onOk={() => void continueSubmit()}
        onCancel={() => setConfirming(false)}
      >
        <Typography.Paragraph type="secondary">
          这些提醒不会阻止训练开始，但可能影响效果或排队时间。
        </Typography.Paragraph>
        {preflight && <PreflightList checks={preflight.checks} />}
      </Modal>

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
                  <ParamsPanel
                    key={selectedRecipe.id}
                    form={form}
                    recipe={selectedRecipe}
                    defaults={recipeInitialParams(selectedRecipe)}
                  />
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