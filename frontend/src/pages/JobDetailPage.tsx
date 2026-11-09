import { Alert, Button, Card, Col, Descriptions, Empty, List, Row, Space, Spin, Tag, Typography } from 'antd';
import { ArrowDownOutlined, ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { JobActions } from '../components/JobActions';
import { PriorityTag, StatusTag } from '../components/StatusTag';
import { usePolling } from '../hooks/usePolling';
import type { Job, JobEventsResponse, JobLogs } from '../types/api';
import { formatDateTime, formatDuration } from '../utils/format';

export function JobDetailPage() {
  const { name = '' } = useParams();
  const [job, setJob] = useState<Job | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [jobLoading, setJobLoading] = useState(true);
  const [logs, setLogs] = useState<JobLogs | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(true);
  const [events, setEvents] = useState<JobEventsResponse | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [following, setFollowing] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const refreshJob = usePolling(async (signal) => {
    try {
      const data = await api.getJob(name, signal);
      setJob(data);
      setJobError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setJobError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setJobLoading(false);
    }
  });

  const refreshLogs = usePolling(async (signal) => {
    try {
      const data = await api.getJobLogs(name, 500, signal);
      setLogs(data);
      setLogError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setLogError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLogsLoading(false);
    }
  });

  // 事件请求独立捕获异常，失败时只影响事件区块，不打断详情和日志
  const refreshEvents = usePolling(async (signal) => {
    try {
      const data = await api.getJobEvents(name, signal);
      setEvents(data);
      setEventsError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setEventsError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setEventsLoading(false);
    }
  });

  useEffect(() => {
    if (!followRef.current) return;
    const frame = requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [logs?.logs]);

  const handleLogScroll = () => {
    const element = logRef.current;
    if (!element) return;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
    followRef.current = atBottom;
    setFollowing(atBottom);
  };

  const scrollToBottom = () => {
    followRef.current = true;
    setFollowing(true);
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  };

  const refreshAll = () => {
    refreshJob();
    refreshLogs();
    refreshEvents();
  };

  return (
    <div>
      <div className="page-heading">
        <div>
          <Link to="/jobs"><ArrowLeftOutlined /> 返回任务列表</Link>
          <Typography.Title level={2} className="detail-title">{name} {job && <StatusTag status={job.status} />}</Typography.Title>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refreshAll}>刷新</Button>
          {job && <JobActions job={job} onChanged={refreshJob} size="middle" />}
        </Space>
      </div>
      <ErrorAlert error={jobError} onRetry={refreshJob} title="任务详情加载失败" />
      <Spin spinning={jobLoading && !job}>
        {job ? (
          <>
            {job.message && (
              <Alert
                className="job-message"
                showIcon
                type={job.status === 'FAILED' ? 'error' : job.status === 'SUCCEEDED' ? 'success' : 'info'}
                message="事件 / 消息"
                description={job.message}
              />
            )}
            <Card title="提交参数" className="section-card">
              <Descriptions bordered column={{ xs: 1, md: 2 }} size="small">
                <Descriptions.Item label="任务名称">{job.name}</Descriptions.Item>
                <Descriptions.Item label="当前状态"><StatusTag status={job.status} /></Descriptions.Item>
                <Descriptions.Item label="训练镜像" span={2}><Typography.Text copyable code>{job.image}</Typography.Text></Descriptions.Item>
                <Descriptions.Item label="GPU 数量">{job.gpus} 卡</Descriptions.Item>
                <Descriptions.Item label="优先级"><PriorityTag priority={job.priority} /></Descriptions.Item>
                <Descriptions.Item label="Kueue 队列">{job.queue}</Descriptions.Item>
                <Descriptions.Item label="失败重启次数">{job.max_retries}</Descriptions.Item>
                <Descriptions.Item label="取消宽限期">{`${job.termination_grace_period_seconds ?? 30} 秒`}</Descriptions.Item>
                <Descriptions.Item label="训练命令" span={2}><Typography.Text code className="command-text">{job.command}</Typography.Text></Descriptions.Item>
                <Descriptions.Item label="环境变量" span={2}>
                  {Object.keys(job.env).length ? <pre className="env-display">{Object.entries(job.env).map(([key, value]) => `${key}=${value}`).join('\n')}</pre> : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="Checkpoint 目录" span={2}><Typography.Text copyable code>{job.checkpoint_dir}</Typography.Text></Descriptions.Item>
              </Descriptions>
            </Card>

            <Row gutter={[16, 16]} className="section-card">
              <Col xs={24} md={12}>
                <Card title="Kueue / PyTorchJob 状态" className="full-height-card">
                  <Descriptions column={1}>
                    <Descriptions.Item label="Workload"><Tag color="blue">{job.workload_status || '暂无'}</Tag></Descriptions.Item>
                    <Descriptions.Item label="训练状态"><StatusTag status={job.status} /></Descriptions.Item>
                    <Descriptions.Item label="排队队列">{job.queue}</Descriptions.Item>
                  </Descriptions>
                </Card>
              </Col>
              <Col xs={24} md={12}>
                <Card title="Pod 与运行时间" className="full-height-card">
                  <Descriptions column={1}>
                    <Descriptions.Item label="Pod 状态"><Tag color={job.pod_status === 'Running' ? 'processing' : 'default'}>{job.pod_status || '尚未创建'}</Tag></Descriptions.Item>
                    <Descriptions.Item label="提交时间">{formatDateTime(job.created_at)}</Descriptions.Item>
                    <Descriptions.Item label="开始时间">{formatDateTime(job.started_at)}</Descriptions.Item>
                    <Descriptions.Item label="结束时间">{formatDateTime(job.finished_at)}</Descriptions.Item>
                    <Descriptions.Item label="运行时长">{formatDuration(job.duration_seconds)}</Descriptions.Item>
                  </Descriptions>
                </Card>
              </Col>
            </Row>
          </>
        ) : !jobLoading && !jobError ? <Empty description="未找到任务详情" /> : null}
      </Spin>

      <Card
        title="事件"
        className="section-card"
        extra={events && <Tag color={events.items.length ? 'blue' : 'default'}>{`${events.items.length} 条`}</Tag>}
      >
        <ErrorAlert error={eventsError} onRetry={refreshEvents} title="事件加载失败" />
        <Spin spinning={eventsLoading && !events}>
          {events && events.items.length > 0 ? (
            <List
              dataSource={events.items}
              renderItem={(event) => {
                const warning = event.type === 'Warning';
                return (
                  <List.Item className={warning ? 'event-item-warning' : undefined}>
                    <div className="event-row">
                      <Space size={[8, 4]} wrap>
                        <Tag color={warning ? 'error' : 'default'}>{event.type || '提示'}</Tag>
                        <Typography.Text strong type={warning ? 'danger' : undefined}>
                          {event.reason || '未知原因'}
                        </Typography.Text>
                        <Typography.Text type="secondary">
                          {event.object_kind ? `${event.object_kind}/${event.object_name || '—'}` : '—'}
                        </Typography.Text>
                      </Space>
                      <Typography.Paragraph className="event-message" type={warning ? 'danger' : undefined}>
                        {event.message || '无详细描述'}
                      </Typography.Paragraph>
                      <Space size={16} wrap>
                        <Typography.Text type="secondary">出现次数：{event.count}</Typography.Text>
                        <Typography.Text type="secondary">最近发生：{formatDateTime(event.last_seen)}</Typography.Text>
                      </Space>
                    </div>
                  </List.Item>
                );
              }}
            />
          ) : events ? (
            <Empty description="暂无事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <div className="event-placeholder" />
          )}
        </Spin>
      </Card>

      <Card
        title="训练日志"
        className="section-card"
        extra={<Space><Tag color={following ? 'processing' : 'default'}>{following ? '自动跟随' : '已暂停跟随'}</Tag>{!following && <Button size="small" icon={<ArrowDownOutlined />} onClick={scrollToBottom}>回到底部</Button>}</Space>}
      >
        <ErrorAlert error={logError} onRetry={refreshLogs} title="日志加载失败" />
        <Typography.Paragraph type="secondary">
          Pod：{logs?.pod_name || '尚未创建'} · 每次获取末尾 500 行
        </Typography.Paragraph>
        <Spin spinning={logsLoading && !logs}>
          <div className="log-viewer" ref={logRef} onScroll={handleLogScroll}>
            <pre>{logs?.logs || (logs?.pod_name ? '当前 Pod 暂无日志输出' : '任务尚未分配训练 Pod，日志将在 Pod 创建后显示')}</pre>
          </div>
        </Spin>
      </Card>
    </div>
  );
}
