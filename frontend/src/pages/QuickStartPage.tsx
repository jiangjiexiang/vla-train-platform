import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Form,
  Input,
  Modal,
  Radio,
  Row,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
} from 'antd';
import {
  DatabaseOutlined,
  FireOutlined,
  ReloadOutlined,
  RocketOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { api, getErrorMessage, isAbortError, isUnauthorized } from '../api/client';
import { isDatasetUsable } from '../components/DatasetBadges';
import { ErrorAlert } from '../components/ErrorAlert';
import { visibleParams } from '../components/TrainParamsForm';
import type {
  DatasetMeta,
  PreflightResponse,
  Priority,
  Recipe,
  RunRequest,
} from '../types/api';
import { ParamsPanel } from './quickstart/ParamsPanel';
import { PreflightList } from './quickstart/PreflightList';
import { DatasetCard, RecipeCard } from './quickstart/SelectionCards';
import {
  PRIORITY_OPTIONS,
  buildOverrides,
  describePreflightErrors,
  describeSubmitError,
  isRecipeCompatible,
  recipeInitialParams,
  recipeTemplate,
} from './quickstart/utils';

// 页面测试与外部引用统一走本模块再导出，避免各自深引 utils 路径。
export {
  PRIORITY_OPTIONS,
  buildOverrides,
  describePreflightErrors,
  describeSubmitError,
  isRecipeCompatible,
  recipeInitialParams,
  recipeTemplate,
};
export { PreflightList } from './quickstart/PreflightList';

/**
 * 页面间传递「用此数据聚集开始训练」的预选。走 history state 而不是 query，
 * 刷新后不会残留一个可能已被删除的 dataset id。
 */
export interface QuickStartLocationState {
  datasetId?: string;
}

const STEPS = [
  { title: '选数据集', description: '确认格式与规模' },
  { title: '选配方', description: '现成的训练方案' },
  { title: '加入队列', description: '平台自动补全参数' },
];

export function QuickStartPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { message, modal } = App.useApp();
  const preselectedId = (location.state as QuickStartLocationState | null)?.datasetId;

  const [datasets, setDatasets] = useState<DatasetMeta[]>([]);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [loadingDatasets, setLoadingDatasets] = useState(true);
  const [loadingRecipes, setLoadingRecipes] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [datasetError, setDatasetError] = useState<string | null>(null);
  const [recipeError, setRecipeError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [datasetIds, setDatasetIds] = useState<string[]>(preselectedId ? [preselectedId] : []);
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
    setDatasetIds((current) =>
      current.filter((id) => {
        const found = items.find((item) => item.id === id);
        return Boolean(found && isDatasetUsable(found));
      }),
    );
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

  const selectedDatasets = useMemo(
    () => datasetIds.map((id) => datasets.find((item) => item.id === id)).filter((d): d is DatasetMeta => Boolean(d)),
    [datasets, datasetIds],
  );
  const selectedDataset = selectedDatasets[0] ?? null;

  const compatibleRecipes = useMemo(
    () =>
      recipes.filter((recipe) =>
        selectedDatasets.length === 0
          ? isRecipeCompatible(recipe, null)
          : selectedDatasets.every((ds) => isRecipeCompatible(recipe, ds)),
      ),
    [recipes, selectedDatasets],
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
      ...(datasetIds.length > 1 ? { dataset_ids: datasetIds } : {}),
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
      if (!isUnauthorized(reason)) {
        const info = describeSubmitError(reason);
        setSubmitError(info);
        if (/配额不足/.test(info.title)) {
          modal.warning({ title: info.title, content: info.description, okText: '知道了' });
        }
      }
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
                  selected={datasetIds.includes(dataset.id)}
                  onSelect={() =>
                    setDatasetIds((prev) =>
                      prev.includes(dataset.id) ? prev.filter((id) => id !== dataset.id) : [...prev, dataset.id],
                    )
                  }
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
