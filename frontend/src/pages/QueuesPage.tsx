import { Button, Card, Col, Empty, Progress, Row, Space, Spin, Statistic, Table, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { PriorityTag, StatusTag } from '../components/StatusTag';
import { usePolling } from '../hooks/usePolling';
import type { Queue, QueueItem } from '../types/api';
import { formatDateTime, percent } from '../utils/format';

const itemColumns: ColumnsType<QueueItem> = [
  {
    title: '任务名称',
    key: 'name',
    render: (_, item) => {
      const name = item.job_name || item.name;
      return <Link to={`/jobs/${encodeURIComponent(name)}`}>{name}</Link>;
    },
  },
  { title: '状态', dataIndex: 'status', key: 'status', render: (status: string) => <StatusTag status={status} /> },
  { title: '优先级', dataIndex: 'priority', key: 'priority', render: (priority: string) => <PriorityTag priority={priority} /> },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', render: formatDateTime },
];

export function QueuesPage() {
  const [queues, setQueues] = useState<Queue[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = usePolling(async (signal) => {
    try {
      const data = await api.getQueues(signal);
      setQueues(data.items);
      setError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  });

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>资源队列</Typography.Title>
          <Typography.Text type="secondary">查看 Kueue GPU 配额、使用量与等待放行的任务</Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </div>
      <ErrorAlert error={error} onRetry={refresh} title="队列信息加载失败" />
      <Spin spinning={loading && queues.length === 0}>
        {queues.length === 0 && !loading ? (
          <Card><Empty description="暂无队列信息，请检查 Kueue 配置" /></Card>
        ) : queues.map((queue) => (
          <Card
            key={queue.name}
            title={<Space>LocalQueue: <Typography.Text strong>{queue.name}</Typography.Text></Space>}
            extra={<Typography.Text type="secondary">ClusterQueue: {queue.cluster_queue}</Typography.Text>}
            className="section-card"
          >
            <Row gutter={[24, 24]} align="middle">
              <Col xs={24} md={8}>
                <Statistic title="GPU 配额 / 已使用" value={`${queue.gpu_used} / ${queue.gpu_quota}`} suffix="卡" />
                <Progress percent={percent(queue.gpu_used, queue.gpu_quota)} status={queue.gpu_used >= queue.gpu_quota && queue.gpu_quota > 0 ? 'exception' : 'normal'} />
              </Col>
              <Col xs={12} md={4}><Statistic title="等待放行" value={queue.pending} /></Col>
              <Col xs={12} md={4}><Statistic title="已放行" value={queue.admitted} /></Col>
              <Col xs={24} md={8}><Typography.Text type="secondary">配额用满时，新任务将保持在排队状态，已有任务结束后自动放行。</Typography.Text></Col>
            </Row>
            <Table<QueueItem>
              rowKey={(item) => item.job_name || item.name}
              columns={itemColumns}
              dataSource={queue.items}
              className="queue-items"
              pagination={false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前队列没有工作负载" /> }}
            />
          </Card>
        ))}
      </Spin>
    </div>
  );
}
