import { Tag, Tooltip, Typography } from 'antd';
import { ExclamationCircleOutlined, WarningOutlined } from '@ant-design/icons';

import type { DatasetIssue, DatasetMeta } from '../types/api';

/** 数据集状态 → 中文文案与标签色（scanner.py 里 status 的全部取值）。 */
export const DATASET_STATUS_META: Record<string, { label: string; color: string }> = {
  ready: { label: '可用', color: 'success' },
  warning: { label: '有警告', color: 'gold' },
  unusable: { label: '不可用', color: 'error' },
  error: { label: '扫描失败', color: 'error' },
  scanning: { label: '扫描中', color: 'processing' },
};

export function datasetStatusMeta(status: string) {
  return DATASET_STATUS_META[status] ?? { label: status || '未知', color: 'default' };
}

/** 状态不可用（unusable / error）的数据集不允许被一键训练选中。 */
export function isDatasetUsable(dataset: Pick<DatasetMeta, 'status'>): boolean {
  return dataset.status !== 'unusable' && dataset.status !== 'error';
}

export function issuesByLevel(issues: DatasetIssue[] | undefined, level: string): DatasetIssue[] {
  return (issues ?? []).filter((issue) => issue.level === level);
}

/** 状态徽标：不可用/有警告时附带图标，便于在网格里一眼扫到。 */
export function DatasetStatusTag({ status }: { status: string }) {
  const meta = datasetStatusMeta(status);
  const icon = status === 'unusable' || status === 'error'
    ? <ExclamationCircleOutlined />
    : status === 'warning'
      ? <WarningOutlined />
      : null;
  return (
    <Tag color={meta.color} icon={icon} data-testid={`dataset-status-${status}`}>
      {meta.label}
    </Tag>
  );
}

/** 把 issues 渲染成可读列表，按 level 区分颜色。 */
export function DatasetIssueList({ issues, emptyText = '没有发现问题' }: { issues: DatasetIssue[]; emptyText?: string }) {
  if (issues.length === 0) {
    return <Typography.Text type="secondary">{emptyText}</Typography.Text>;
  }
  const colorOf = (level: string): 'danger' | 'warning' | 'secondary' =>
    level === 'error' ? 'danger' : level === 'warning' ? 'warning' : 'secondary';
  return (
    <ul className="dataset-issue-list">
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${index}`} className={`dataset-issue dataset-issue-${issue.level}`}>
          <Typography.Text type={colorOf(issue.level)}>
            [{issue.level}] {issue.message}
          </Typography.Text>
          <Typography.Text type="secondary" className="dataset-issue-code">{issue.code}</Typography.Text>
        </li>
      ))}
    </ul>
  );
}

/** 卡片右上角的警告图标：hover 列出全部 warning。 */
export function DatasetWarningHint({ issues }: { issues: DatasetIssue[] }) {
  const warnings = issuesByLevel(issues, 'warning');
  if (warnings.length === 0) return null;
  return (
    <Tooltip
      title={(
        <div>
          {warnings.map((issue, index) => (
            <div key={`${issue.code}-${index}`}>· {issue.message}</div>
          ))}
        </div>
      )}
    >
      <WarningOutlined className="dataset-warning-hint" aria-label="数据集警告" />
    </Tooltip>
  );
}