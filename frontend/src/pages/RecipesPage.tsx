import { Alert, Button, Card, Collapse, Empty, Space, Spin, Table, Tag, Typography } from 'antd';
import { ExperimentOutlined, RocketOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';

import { api, getErrorMessage, isAbortError, isUnauthorized } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import type { ParamSpec, Recipe } from '../types/api';
import { formatParamDefault } from '../utils/dataset';

const PARAM_COLUMNS: ColumnsType<ParamSpec> = [
  { title: '参数', dataIndex: 'key', key: 'key', width: 160, render: (key: string) => <Typography.Text code>{key}</Typography.Text> },
  { title: '名称', dataIndex: 'label', key: 'label', width: 160 },
  { title: '命令行', dataIndex: 'flag', key: 'flag', render: (flag: string | null) => (flag ? <Typography.Text code>{flag}</Typography.Text> : '—') },
  { title: '类型', dataIndex: 'type', key: 'type', width: 80 },
  {
    title: '默认值',
    dataIndex: 'default',
    key: 'default',
    width: 120,
    render: (value: unknown) => formatParamDefault(value),
  },
  {
    title: '范围',
    key: 'range',
    width: 140,
    render: (_, param) => {
      if (param.options && param.options.length > 0) return param.options.join(' / ');
      if (param.min !== undefined || param.max !== undefined) return `${param.min ?? '-∞'} ~ ${param.max ?? '+∞'}`;
      return '—';
    },
  },
  {
    title: '说明',
    dataIndex: 'help',
    key: 'help',
    render: (help: string | undefined, param) => (
      <Space direction="vertical" size={0}>
        {param.required && <Tag color="red">必填</Tag>}
        <Typography.Text type="secondary">{help || '—'}</Typography.Text>
      </Space>
    ),
  },
];

function RecipeDetail({ recipe }: { recipe: Recipe }) {
  const params = recipe.params ?? [];
  return (
    <div className="recipe-detail">
      <Typography.Paragraph type="secondary">{recipe.description || '暂无描述'}</Typography.Paragraph>
      <Space size={16} wrap className="recipe-detail-meta">
        <span><Typography.Text type="secondary">镜像：</Typography.Text><Typography.Text code>{recipe.image || '未配置'}</Typography.Text></span>
        <span><Typography.Text type="secondary">入口：</Typography.Text><Typography.Text code>{recipe.entrypoint || '—'}</Typography.Text></span>
        <span>
          <Typography.Text type="secondary">支持格式：</Typography.Text>
          {(recipe.dataset_formats ?? []).map((format) => (
            <Tag key={format} bordered={false}>{format}</Tag>
          ))}
        </span>
      </Space>
      <Typography.Title level={5}>参数清单（{params.length}）</Typography.Title>
      {params.length === 0 ? (
        <Typography.Text type="secondary">该配方没有声明参数。</Typography.Text>
      ) : (
        <Table<ParamSpec>
          rowKey="key"
          size="small"
          pagination={false}
          columns={PARAM_COLUMNS}
          dataSource={params}
        />
      )}
    </div>
  );
}

export function RecipesPage() {
  const navigate = useNavigate();
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listRecipes();
      setRecipes(data.items ?? []);
      setError(null);
    } catch (reason) {
      if (isAbortError(reason) || isUnauthorized(reason)) return;
      setError(getErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // 配方是随镜像发布的只读配置，进入页面拉一次即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>训练配方</Typography.Title>
          <Typography.Text type="secondary">
            配方把镜像、参数和默认值打包成一键可用的训练方案；这里只能浏览，修改请联系管理员。
          </Typography.Text>
        </div>
        <Button type="primary" icon={<RocketOutlined />} onClick={() => navigate('/')}>
          去快速开始
        </Button>
      </div>

      <ErrorAlert error={error} onRetry={() => void load()} title="配方加载失败" />

      {!loading && !error && recipes.length === 0 && (
        <Card className="section-card">
          <Empty description="还没有登记任何训练配方" />
        </Card>
      )}

      <Spin spinning={loading && recipes.length === 0}>
        {recipes.length > 0 && (
          <Collapse
            className="recipe-collapse"
            items={recipes.map((recipe) => ({
              key: recipe.id,
              label: (
                <Space size={8} wrap className="recipe-collapse-label">
                  <ExperimentOutlined />
                  <Typography.Text strong>{recipe.name}</Typography.Text>
                  {recipe.estimated_vram_gb !== null && recipe.estimated_vram_gb !== undefined && (
                    <Tag icon={<ThunderboltOutlined />} bordered={false}>约 {recipe.estimated_vram_gb} GB 显存</Tag>
                  )}
                  <Tag bordered={false}>{recipe.gpu_count} × GPU</Tag>
                  {recipe.dataset_formats.map((format) => <Tag key={format} color="blue">{format}</Tag>)}
                  <Typography.Text type="secondary" ellipsis className="recipe-collapse-desc">
                    {recipe.description}
                  </Typography.Text>
                </Space>
              ),
              children: <RecipeDetail recipe={recipe} />,
            }))}
          />
        )}
      </Spin>

      {recipes.length > 0 && (
        <Alert
          type="info"
          showIcon
          className="section-card recipe-footnote"
          message="怎么用？"
          description="在「快速开始」里选一个数据集，平台会列出所有格式兼容的配方，选中后点「加入训练队列」即可。"
        />
      )}
    </div>
  );
}