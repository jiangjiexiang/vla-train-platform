import { Button, Card, Col, Empty, Progress, Row, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import { CloudServerOutlined, ReloadOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { usePolling } from '../hooks/usePolling';
import { useAuth } from '../context/AuthContext';
import type { ClusterNode, ClusterSummary, FailureReason, FailureSummary, GpuMetric } from '../types/api';
import { percent } from '../utils/format';

const nodeColumns: ColumnsType<ClusterNode> = [
  { title: '节点名称', dataIndex: 'name', key: 'name', render: (name: string) => <><CloudServerOutlined /> {name}</> },
  {
    title: '节点状态',
    dataIndex: 'ready',
    key: 'ready',
    render: (ready: boolean) => <Tag color={ready ? 'success' : 'error'}>{ready ? '就绪' : '未就绪'}</Tag>,
  },
  { title: 'GPU 总数', dataIndex: 'gpu_total', key: 'gpu_total' },
  { title: '已分配 GPU', dataIndex: 'gpu_allocated', key: 'gpu_allocated' },
  {
    title: '空闲 GPU',
    key: 'available',
    render: (_, node) => Math.max(0, node.gpu_total - node.gpu_allocated),
  },
];

const gpuMetricColumns: ColumnsType<GpuMetric> = [
  {
    title: 'GPU',
    key: 'name',
    render: (_, metric) => (
      <div>
        <Typography.Text strong>GPU {metric.index}</Typography.Text>
        <br />
        <Typography.Text type="secondary">{metric.name}</Typography.Text>
      </div>
    ),
  },
  {
    title: '核心利用率',
    dataIndex: 'utilization_percent',
    key: 'utilization',
    render: (value: number) => <Progress percent={value} size="small" />,
  },
  {
    title: '显存占用',
    key: 'memory',
    render: (_, metric) => (
      <div>
        <Progress percent={percent(metric.memory_used_mib, metric.memory_total_mib)} size="small" />
        <Typography.Text type="secondary">
          {(metric.memory_used_mib / 1024).toFixed(1)} / {(metric.memory_total_mib / 1024).toFixed(1)} GiB
        </Typography.Text>
      </div>
    ),
  },
  {
    title: '温度',
    dataIndex: 'temperature_celsius',
    key: 'temperature',
    render: (value: number) => `${value} °C`,
  },
];

export function DashboardPage() {
  const [summary, setSummary] = useState<ClusterSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = usePolling(async (signal) => {
    try {
      const data = await api.getClusterSummary(signal);
      setSummary(data);
      setError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  });

  const jobs = summary?.jobs;
  // 失败原因分布：运维判断「是不是平台在制造失败」的依据。
  // 只对管理员展示——聚合结果会暴露他人的失败情况。
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
  const navigate = useNavigate();
  const [failures, setFailures] = useState<FailureSummary | null>(null);
  const [failuresLoading, setFailuresLoading] = useState(false);
  const loadFailures = useCallback(async () => {
    if (!isAdmin) return;
    setFailuresLoading(true);
    try {
      setFailures(await api.getFailureSummary());
    } catch {
      // 聚合失败不该让整个总览页报错，静静地留空即可
      setFailures(null);
    } finally {
      setFailuresLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    void loadFailures();
  }, [loadFailures]);

  const failureColumns: ColumnsType<FailureReason> = useMemo(
    () => [
      {
        title: '原因',
        dataIndex: 'summary',
        render: (_, row) => (
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{row.summary || row.code}</Typography.Text>
            <Typography.Text type="secondary" className="hint">{row.suggestion}</Typography.Text>
          </Space>
        ),
      },
      {
        title: '次数',
        dataIndex: 'count',
        width: 80,
        render: (count: number) => <Tag color={count > 3 ? 'red' : 'orange'}>{count}</Tag>,
      },
      {
        title: '示例',
        dataIndex: 'example_job',
        width: 200,
        render: (job: string) => (
          <Typography.Link onClick={() => navigate(`/jobs/${job}`)}>{job}</Typography.Link>
        ),
      },
    ],
    [navigate],
  );

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>集群总览</Typography.Title>
          <Typography.Text type="secondary">实时查看 GPU 资源、节点和训练任务状态</Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </div>
      <ErrorAlert error={error} onRetry={refresh} title="无法获取集群总览，请检查集群与后端服务" />
      <Spin spinning={loading && !summary}>
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={8}>
            <Card><Statistic title="GPU 总数" value={summary?.gpu_total ?? '—'} suffix="卡" /></Card>
          </Col>
          <Col xs={24} sm={8}>
            <Card><Statistic title="已使用 GPU" value={summary?.gpu_allocated ?? '—'} suffix="卡" valueStyle={{ color: '#1677ff' }} /></Card>
          </Col>
          <Col xs={24} sm={8}>
            <Card><Statistic title="空闲 GPU" value={summary?.gpu_available ?? '—'} suffix="卡" valueStyle={{ color: '#52c41a' }} /></Card>
          </Col>
        </Row>

        <Card title="GPU 分配情况" className="section-card">
          {summary ? (
            <Row align="middle" gutter={24}>
              <Col flex="120px"><Progress type="dashboard" percent={percent(summary.gpu_allocated, summary.gpu_total)} /></Col>
              <Col flex="auto">
                <Typography.Text strong>{summary.gpu_allocated} / {summary.gpu_total} 卡已分配</Typography.Text>
                <br />
                <Typography.Text type="secondary">共 {summary.node_count} 个节点，资源数据来自 Kubernetes</Typography.Text>
              </Col>
            </Row>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无集群数据" />}
        </Card>

        <Card title="GPU 实时使用情况" className="section-card">
          {summary?.gpu_metrics?.available ? (
            <Table<GpuMetric>
              rowKey="index"
              columns={gpuMetricColumns}
              dataSource={summary.gpu_metrics.items}
              pagination={false}
              locale={{ emptyText: <Empty description="未检测到 GPU 实时指标" /> }}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="实时指标暂不可用，请检查 vla-gpu-metrics 服务" />
          )}
        </Card>

        <Card title="任务统计" className="section-card">
          <Row gutter={[16, 16]}>
            <Col xs={12} md={4}><Statistic title="排队中" value={jobs?.queued ?? 0} valueStyle={{ color: '#d48806' }} /></Col>
            <Col xs={12} md={4}><Statistic title="运行中" value={jobs?.running ?? 0} valueStyle={{ color: '#1677ff' }} /></Col>
            <Col xs={12} md={4}><Statistic title="已成功" value={jobs?.succeeded ?? 0} valueStyle={{ color: '#52c41a' }} /></Col>
            <Col xs={12} md={4}><Statistic title="已失败" value={jobs?.failed ?? 0} valueStyle={{ color: '#cf1322' }} /></Col>
            <Col xs={12} md={4}><Statistic title="已取消" value={jobs?.cancelled ?? 0} /></Col>
            <Col xs={12} md={4}><Statistic title="状态未知" value={jobs?.unknown ?? 0} /></Col>
          </Row>
        </Card>

        <Card
          title="失败原因分布"
          className="section-card"
          extra={<Button size="small" onClick={refresh}>刷新</Button>}
        >
          {failures && failures.total_failed === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="最近没有失败任务" />
          ) : (
            <Table<FailureReason>
              rowKey="code"
              size="small"
              columns={failureColumns}
              dataSource={failures?.reasons || []}
              pagination={false}
              loading={failuresLoading}
              locale={{ emptyText: <Empty description="暂无失败记录" /> }}
            />
          )}
          {failures && failures.unknown > 0 ? (
            <Typography.Text type="secondary" className="hint">
              还有 {failures.unknown} 个失败任务没有诊断结论（日志可能已被回收）
            </Typography.Text>
          ) : null}
        </Card>

        <Card title={`节点 (${summary?.nodes.length ?? 0})`} className="section-card">
          <Table<ClusterNode>
            rowKey="name"
            columns={nodeColumns}
            dataSource={summary?.nodes || []}
            pagination={false}
            locale={{ emptyText: <Empty description="暂无节点信息" /> }}
          />
        </Card>
      </Spin>
    </div>
  );
}
