import { CheckCircleFilled, ExclamationCircleOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Space, Tag, Tooltip, Typography } from 'antd';

import {
  DatasetStatusTag,
  DatasetWarningHint,
  isDatasetUsable,
  issuesByLevel,
} from '../../components/DatasetBadges';
import type { DatasetMeta, Recipe } from '../../types/api';
import { formatBytes, formatCount, formatMinutes } from '../../utils/dataset';

export function DatasetCard({
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

export function RecipeCard({
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
