import {
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  DatabaseOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';

import { api, getErrorMessage, isAbortError, isUnauthorized } from '../api/client';
import {
  DatasetIssueList,
  DatasetStatusTag,
  isDatasetUsable,
} from '../components/DatasetBadges';
import { ErrorAlert } from '../components/ErrorAlert';
import type { DatasetListResponse, DatasetMeta } from '../types/api';
import { formatBytes, formatCount, formatMinutes } from '../utils/dataset';
import { formatDateTime } from '../utils/format';

function imagesText(item: DatasetMeta): string {
  if (item.image_size && item.image_size.length >= 2) {
    return `${item.image_size[1]}×${item.image_size[0]}`;
  }
  return '—';
}

function listText(values: string[] | undefined, emptyText = '—'): string {
  return values && values.length > 0 ? values.join(', ') : emptyText;
}

export function DatasetsPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<DatasetListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [active, setActive] = useState<DatasetMeta | null>(null);

  const load = async (refresh = false) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    try {
      const response = await api.listDatasets(refresh);
      setData(response);
      setError(null);
      // 抽屉里的数据同步刷新后的最新版本，避免展示过期元数据
      setActive((current) =>
        current ? response.items?.find((item) => item.id === current.id) ?? null : null);
    } catch (reason) {
      if (isAbortError(reason) || isUnauthorized(reason)) return;
      setError(getErrorMessage(reason));
    } finally {
      setRefreshing(false);
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(false);
    // 进入页面只拉一次缓存，「重新扫描」按钮显式触发 refresh=true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const items = useMemo(() => data?.items ?? [], [data]);
  const filtered = useMemo(() => {
    const text = keyword.trim().toLowerCase();
    if (!text) return items;
    return items.filter((item) =>
      item.name.toLowerCase().includes(text) || item.id.toLowerCase().includes(text));
  }, [items, keyword]);

  const summary = data?.summary;
  const columns: ColumnsType<DatasetMeta> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      fixed: 'left',
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.name}</Typography.Text>
          <Typography.Text type="secondary" className="cell-secondary">{item.path}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <DatasetStatusTag status={status} />,
    },
    {
      title: '格式',
      dataIndex: 'format',
      key: 'format',
      width: 130,
      render: (format: string) => <Tag bordered={false}>{format || 'unknown'}</Tag>,
    },
    {
      title: '集数',
      dataIndex: 'episodes',
      key: 'episodes',
      width: 90,
      align: 'right',
      render: (value: number) => formatCount(value),
    },
    {
      title: '帧数',
      dataIndex: 'frames',
      key: 'frames',
      width: 110,
      align: 'right',
      render: (value: number) => formatCount(value),
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      key: 'duration',
      width: 110,
      render: (value: number) => formatMinutes(value),
    },
    {
      title: '相机',
      dataIndex: 'cameras',
      key: 'cameras',
      width: 80,
      render: (cameras: string[]) => (
        <Tooltip title={listText(cameras)}>{cameras?.length ?? 0} 路</Tooltip>
      ),
    },
    {
      title: '维度 (state / action)',
      key: 'dims',
      width: 150,
      render: (_, item) => `${item.state_dim} / ${item.action_dim}`,
    },
    {
      title: '体积',
      dataIndex: 'size_bytes',
      key: 'size',
      width: 100,
      align: 'right',
      render: (value: number) => formatBytes(value),
    },
    {
      title: '扫描时间',
      dataIndex: 'scanned_at',
      key: 'scanned_at',
      width: 180,
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      fixed: 'right',
      render: (_, item) => (
        <Button
          type="link"
          size="small"
          icon={<PlayCircleOutlined />}
          disabled={!isDatasetUsable(item)}
          onClick={(event) => {
            event.stopPropagation();
            navigate('/', { state: { datasetId: item.id } });
          }}
        >
          用它训练
        </Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>数据集</Typography.Title>
          <Typography.Text type="secondary">
            扫描自 {data?.root || '/datasets'}，不可用的数据集不会出现在一键训练的候选里。
          </Typography.Text>
        </div>
        <Space size={8}>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索数据集名称"
            className="job-search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <Button
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={() => void load(true)}
            data-testid="rescan-button"
          >
            重新扫描
          </Button>
        </Space>
      </div>

      {summary && (
        <Card className="section-card" size="small">
          <Row gutter={16}>
            <Col xs={12} md={6}><Statistic title="数据集总数" value={summary.total} prefix={<DatabaseOutlined />} /></Col>
            <Col xs={12} md={6}><Statistic title="可用" value={summary.ready} valueStyle={{ color: '#389e0d' }} /></Col>
            <Col xs={12} md={6}><Statistic title="有警告" value={summary.warning} valueStyle={{ color: '#d48806' }} /></Col>
            <Col xs={12} md={6}>
              <Statistic title="不可用" value={summary.unusable} valueStyle={{ color: summary.unusable > 0 ? '#cf1322' : undefined }} />
            </Col>
          </Row>
          <Typography.Text type="secondary" className="summary-footnote">
            累计 {formatCount(summary.total_frames)} 帧
          </Typography.Text>
        </Card>
      )}

      <ErrorAlert error={error} onRetry={() => void load(true)} title="数据集加载失败" />

      <Card className="section-card" title={<Space><FileSearchOutlined /> 数据集列表</Space>}>
        <Table<DatasetMeta>
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={filtered}
          scroll={{ x: 1400 }}
          pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty description="没有数据集" /> }}
          onRow={(record) => ({
            onClick: () => setActive(record),
            style: { cursor: 'pointer' },
          })}
        />
      </Card>

      <Drawer
        open={active !== null}
        width={620}
        onClose={() => setActive(null)}
        title={active ? (
          <Space>
            <span>{active.name}</span>
            <DatasetStatusTag status={active.status} />
          </Space>
        ) : null}
        extra={active && (
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            disabled={!isDatasetUsable(active)}
            onClick={() => navigate('/', { state: { datasetId: active.id } })}
          >
            用此数据集开始训练
          </Button>
        )}
      >
        {active && (
          <Space direction="vertical" size={20} style={{ width: '100%' }}>
            <Descriptions column={1} size="small" bordered title="基本元数据">
              <Descriptions.Item label="ID">{active.id}</Descriptions.Item>
              <Descriptions.Item label="路径">{active.path}</Descriptions.Item>
              <Descriptions.Item label="格式">{active.format || 'unknown'}</Descriptions.Item>
              <Descriptions.Item label="集数 / 帧数">
                {formatCount(active.episodes)} 集 / {formatCount(active.frames)} 帧
              </Descriptions.Item>
              <Descriptions.Item label="时长">{formatMinutes(active.duration_seconds)}</Descriptions.Item>
              <Descriptions.Item label="帧率">{active.fps ? `${active.fps} FPS` : '—'}</Descriptions.Item>
              <Descriptions.Item label="体积">{formatBytes(active.size_bytes)}</Descriptions.Item>
              <Descriptions.Item label="机器人类别">{active.robot_type || '—'}</Descriptions.Item>
              <Descriptions.Item label="任务描述">{listText(active.tasks)}</Descriptions.Item>
              <Descriptions.Item label="相机">
                {active.cameras && active.cameras.length > 0
                  ? active.cameras.map((camera) => <Tag key={camera}>{camera}</Tag>)
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="图像尺寸">{imagesText(active)}</Descriptions.Item>
              <Descriptions.Item label="state 维度">
                {active.state_dim}
                {active.state_names.length > 0 && (
                  <Typography.Text type="secondary">（{listText(active.state_names)}）</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="action 维度">
                {active.action_dim}
                {active.action_names.length > 0 && (
                  <Typography.Text type="secondary">（{listText(active.action_names)}）</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="扫描">{`${active.scan_depth === 'deep' ? '深扫描' : '浅扫描'} · ${formatDateTime(active.scanned_at)}`}</Descriptions.Item>
              <Descriptions.Item label="目录修改时间">{formatDateTime(
                active.dir_mtime ? new Date(active.dir_mtime * 1000).toISOString() : null)}</Descriptions.Item>
            </Descriptions>

            <div>
              <Typography.Title level={5}>问题清单（{active.issues?.length ?? 0}）</Typography.Title>
              <DatasetIssueList issues={active.issues ?? []} />
            </div>
          </Space>
        )}
      </Drawer>
    </div>
  );
}