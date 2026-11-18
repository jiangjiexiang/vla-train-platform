import {
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  App,
  Empty,
  Form,
  Input,
  Modal,
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
  PlusOutlined,
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
  const { message } = App.useApp();
  const [active, setActive] = useState<DatasetMeta | null>(null);
  const [registerOpen, setRegisterOpen] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [regForm] = Form.useForm<{ path: string; id?: string }>();

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
      width: 280,
      render: (_, item) => (
        <Space direction="vertical" size={2} style={{ maxWidth: 256 }}>
          <Typography.Text strong ellipsis={{ tooltip: item.name }}>{item.name}</Typography.Text>
          <Typography.Text type="secondary" className="cell-secondary" ellipsis={{ tooltip: item.path }}>{item.path}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
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
      width: 100,
      align: 'right',
      render: (value: number) => <span style={{ whiteSpace: 'nowrap' }}>{formatCount(value)}</span>,
    },
    {
      title: '帧数',
      dataIndex: 'frames',
      key: 'frames',
      width: 120,
      align: 'right',
      render: (value: number) => <span style={{ whiteSpace: 'nowrap' }}>{formatCount(value)}</span>,
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      key: 'duration',
      width: 140,
      align: 'right',
      render: (value: number) => <span style={{ whiteSpace: 'nowrap' }}>{formatMinutes(value)}</span>,
    },
    {
      title: '相机',
      dataIndex: 'cameras',
      key: 'cameras',
      width: 90,
      align: 'center',
      render: (cameras: string[]) => (
        <Tooltip title={listText(cameras)}><span style={{ whiteSpace: 'nowrap' }}>{cameras?.length ?? 0} 路</span></Tooltip>
      ),
    },
    {
      title: '维度 (state / action)',
      key: 'dims',
      width: 160,
      align: 'center',
      render: (_, item) => <span style={{ whiteSpace: 'nowrap' }}>{`${item.state_dim} / ${item.action_dim}`}</span>,
    },
    {
      title: '体积',
      dataIndex: 'size_bytes',
      key: 'size',
      width: 115,
      align: 'right',
      render: (value: number) => <span style={{ whiteSpace: 'nowrap' }}>{formatBytes(value)}</span>,
    },
    {
      title: '扫描时间',
      dataIndex: 'scanned_at',
      key: 'scanned_at',
      width: 190,
      render: (value: string) => <span style={{ whiteSpace: 'nowrap' }}>{formatDateTime(value)}</span>,
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
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setRegisterOpen(true)}
          >
            注册数据集
          </Button>
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
          loading={loading}
          columns={columns}
          dataSource={filtered}
          scroll={{ x: 1520 }}
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

      <Modal
        title="注册数据集"
        open={registerOpen}
        okText="注册并扫描"
        cancelText="取消"
        confirmLoading={registering}
        onOk={async () => {
          try {
            const values = await regForm.validateFields();
            setRegistering(true);
            await api.registerDataset({
              path: values.path.trim(),
              ...(values.id?.trim() ? { id: values.id.trim() } : {}),
            });
            message.success("数据集注册成功");
            setRegisterOpen(false);
            regForm.resetFields();
            void load(false);
          } catch (reason) {
            if (reason && typeof reason === 'object' && 'errorFields' in reason) return;
            message.error(getErrorMessage(reason));
          } finally {
            setRegistering(false);
          }
        }}
        onCancel={() => {
          setRegisterOpen(false);
          regForm.resetFields();
        }}
      >
        <Form form={regForm} layout="vertical">
          <Form.Item
            name="path"
            label="数据集路径"
            rules={[{ required: true, message: "请输入数据集路径" }]}
            extra="支持容器内路径（如 /datasets/my_data）或宿主机路径（如 /data/vla-platform/datasets/my_data）"
          >
            <Input placeholder="/datasets/my_robot_data" />
          </Form.Item>
          <Form.Item name="id" label="数据集 ID（可选，默认取目录名）">
            <Input placeholder="留空自动使用目录名" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}