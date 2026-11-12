import { Alert, Button, Card, Col, Collapse, Descriptions, Empty, List, Row, Segmented, Space, Spin, Tag, Typography } from 'antd';
import { ArrowDownOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { CheckpointList } from '../../components/CheckpointList';
import { ErrorAlert } from '../../components/ErrorAlert';
import { JobTimeline } from '../../components/JobTimeline';
import { LossChart } from '../../components/LossChart';
import { PriorityTag, StatusTag } from '../../components/StatusTag';
import type {
  Job,
  JobCheckpointsResponse,
  JobDiagnosisResponse,
  JobEventsResponse,
  JobLogs,
  JobMetricsResponse,
  JobTimelineResponse,
} from '../../types/api';
import { formatDateTime, formatDuration, formatQueueEta } from '../../utils/format';
import { metricOptions } from '../../utils/metricOptions';

const EVENT_HINTS: Record<string, string> = {
  PyTorchJobSucceeded: '训练任务正常结束',
  FinishedWorkload: '队列确认任务结束',
  Completed: '容器正常退出',
  Failed: '任务失败',
  BackoffLimitExceeded: '重试次数用尽，任务判定失败',
  OOMKilled: '显存或内存不足，进程被系统强制终止',
  Evicted: '资源紧张，Pod 被驱逐',
  FailedScheduling: '调度失败：集群没有满足要求的 GPU 或资源',
  ErrImagePull: '镜像拉取失败：检查镜像名是否正确、镜像是否已导入',
  ImagePullBackOff: '镜像拉取失败后重试中',
  CrashLoopBackOff: '容器反复崩溃重启',
  FailedMount: '挂载失败：检查数据集或存储路径是否存在',
  Scheduled: '已调度到节点',
  Pulled: '镜像已拉取',
  Created: '容器已创建',
  Started: '容器已启动',
  Killing: '正在停止容器',
  SuccessfulCreate: '创建了训练 Pod',
};

function isBadEvent(type: string | null): boolean {
  return (type ?? '').toLowerCase() === 'warning';
}

export function JobOverview({
  job,
  diagnosis,
  diagnosisError,
  onRetryDiagnosis,
  timeline,
  checkpoints,
}: {
  job: Job;
  diagnosis: JobDiagnosisResponse | null;
  diagnosisError: string | null;
  onRetryDiagnosis: () => void;
  timeline: JobTimelineResponse | null;
  checkpoints: JobCheckpointsResponse | null;
}) {
  const etaInfo = formatQueueEta(job.queue_eta_seconds ?? null, job.queue_eta_source ?? null);

  return (
    <>
      {diagnosis?.diagnosis && (
        <Alert
          className="section-card"
          type="error"
          showIcon
          message={diagnosis.diagnosis.summary}
          description={(
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Text>{`建议：${diagnosis.diagnosis.suggestion}`}</Typography.Text>
              <Collapse
                ghost
                size="small"
                items={[{
                  key: 'evidence',
                  label: '查看原始证据',
                  children: <pre className="env-display">{diagnosis.diagnosis.evidence || '—'}</pre>,
                }]}
              />
            </Space>
          )}
          data-testid="job-diagnosis"
        />
      )}
      {diagnosisError && (
        <ErrorAlert error={diagnosisError} onRetry={onRetryDiagnosis} title="失败诊断加载失败" />
      )}
      {job.queue_state === 'queued' && job.queue_position !== null && job.queue_position !== undefined && etaInfo && (
        <Alert
          className="job-message"
          type="info"
          showIcon
          message={`排队中：排在第 ${job.queue_position} 位${
            job.queue_position > 1 ? `（前面还有 ${job.queue_position - 1} 个任务）` : '（下一个就是你）'
          }，预计等待${etaInfo.text === '暂无法估算' ? '' : ' '}${etaInfo.text}`}
          description={
            etaInfo.approximate
              ? '当前集群没有该配方的历史数据，等待时间按默认假设估算，仅供参考。'
              : undefined
          }
          data-testid="queue-position"
        />
      )}
      {job.message && (
        <Alert
          className="job-message"
          showIcon
          type={job.status === 'FAILED' ? 'error' : job.status === 'SUCCEEDED' ? 'success' : 'info'}
          message="事件 / 消息"
          description={job.message}
        />
      )}
      <JobTimeline data={timeline} />
      <CheckpointList data={checkpoints} />
      <Card title="提交参数" className="section-card">
        <Descriptions bordered column={{ xs: 1, md: 2 }} size="small">
          <Descriptions.Item label="任务名称">{job.name}</Descriptions.Item>
          <Descriptions.Item label="当前状态"><StatusTag status={job.status} /></Descriptions.Item>
          <Descriptions.Item label="提交者">{job.owner || '—'}</Descriptions.Item>
          <Descriptions.Item label="节点数">{`${job.nodes ?? 1} 节点`}</Descriptions.Item>
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
          <Descriptions.Item label="任务标签" span={2}>
            {job.labels && Object.keys(job.labels).length
              ? Object.entries(job.labels).map(([key, value]) => <Tag key={key}>{`${key}=${value}`}</Tag>)
              : '—'}
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
  );
}

function TensorboardFrame({ jobName }: { jobName: string }) {
  const runKey = `${jobName}/tensorboard`;
  const src = `/tensorboard/?runFilter=${encodeURIComponent(runKey)}`;

  return (
    <div>
      <iframe title={`TensorBoard - ${jobName}`} src={src} className="tensorboard-frame" />
      <Typography.Text type="secondary">
        曲线来自 TensorBoard 官方界面，支持平滑、多指标叠加与导出。
      </Typography.Text>
    </div>
  );
}

export function TrainingMetricsCard({
  jobName,
  metrics,
  metricsError,
  onRetry,
}: {
  jobName: string;
  metrics: JobMetricsResponse | null;
  metricsError: string | null;
  onRetry: () => void;
}) {
  const [metricKey, setMetricKey] = useState('loss');
  const [metricSource, setMetricSource] = useState<'platform' | 'tensorboard'>('tensorboard');

  return (
    <Card
      title="训练 Loss 曲线"
      className="section-card"
      extra={(
        <Segmented
          size="small"
          value={metricSource}
          onChange={(value) => setMetricSource(value === 'tensorboard' ? 'tensorboard' : 'platform')}
          options={[
            { label: 'TensorBoard', value: 'tensorboard' },
            { label: '平台指标', value: 'platform' },
          ]}
          data-testid="metric-source-switch"
        />
      )}
    >
      {metricSource === 'platform' ? (
        <>
          <ErrorAlert error={metricsError} onRetry={onRetry} title="训练指标加载失败" />
          {metrics && metrics.items.length > 0 ? (
            <div>
              {metricOptions(metrics.items).length > 0 && (
                <Segmented
                  size="small"
                  value={metricKey}
                  onChange={(value) => setMetricKey(String(value))}
                  options={metricOptions(metrics.items)}
                  data-testid="metric-switch"
                />
              )}
              <LossChart points={metrics.items} metric={metricKey} />
              {metrics.items.length < 10 ? (
                <Typography.Text type="secondary" className="hint">
                  只有 {metrics.items.length} 个点。冒烟测试默认跑 5 步，这是正常的；
                  想看有意义的曲线请用训练配方（默认 2000 步）。
                </Typography.Text>
              ) : null}
            </div>
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={metrics && !metrics.path_exists
                ? '暂无训练指标（训练脚本还没有写出 metrics.jsonl）'
                : '正在加载训练指标…'}
            />
          )}
          <Typography.Paragraph type="secondary" className="chart-hint">
            训练脚本把每步指标写入 /checkpoints/&lt;任务名&gt;/metrics.jsonl，平台从宿主机读取，任务结束后仍可查看。
          </Typography.Paragraph>
        </>
      ) : (
        <TensorboardFrame jobName={jobName} />
      )}
    </Card>
  );
}

export function JobEventsCard({
  events,
  error,
  loading,
  onRetry,
}: {
  events: JobEventsResponse | null;
  error: string | null;
  loading: boolean;
  onRetry: () => void;
}) {
  const warningCount = events
    ? events.items.filter((item) => isBadEvent(item.type)).length
    : 0;
  const hasWarning = warningCount > 0;

  return (
    <Card
      title="事件"
      className="section-card"
      extra={events && (
        <Space>
          <Tag color={hasWarning ? 'error' : 'default'}>
            {hasWarning ? `${warningCount} 条告警` : `${events.items.length} 条`}
          </Tag>
          <Typography.Text type="secondary">
            {hasWarning ? '发现异常，已展开' : '一切正常'}
          </Typography.Text>
        </Space>
      )}
    >
      <ErrorAlert error={error} onRetry={onRetry} title="事件加载失败" />
      <Collapse
        ghost
        size="small"
        defaultActiveKey={hasWarning ? ['events'] : []}
        items={[{
          key: 'events',
          label: hasWarning
            ? `${warningCount} 条告警，${events ? events.items.length - warningCount : 0} 条正常流程事件`
            : '查看事件详情',
          children: (
            <Spin spinning={loading && !events}>
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
                              {EVENT_HINTS[event.reason ?? ''] ?? event.reason ?? '未知原因'}
                            </Typography.Text>
                            <Typography.Text type="secondary">
                              {event.reason && EVENT_HINTS[event.reason] ? `（${event.reason}）` : ''}
                              {event.object_kind ? ` ${event.object_kind}/${event.object_name || '—'}` : ''}
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
          ),
        }]}
      />
    </Card>
  );
}

export function JobLogsCard({
  logs,
  error,
  loading,
  onRetry,
}: {
  logs: JobLogs | null;
  error: string | null;
  loading: boolean;
  onRetry: () => void;
}) {
  const [following, setFollowing] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

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

  return (
    <Card
      title="训练日志"
      className="section-card"
      extra={<Space><Tag color={following ? 'processing' : 'default'}>{following ? '自动跟随' : '已暂停跟随'}</Tag>{!following && <Button size="small" icon={<ArrowDownOutlined />} onClick={scrollToBottom}>回到底部</Button>}</Space>}
    >
      <ErrorAlert error={error} onRetry={onRetry} title="日志加载失败" />
      <Typography.Paragraph type="secondary">
        Pod：{logs?.pod_name || '尚未创建'} · 每次获取末尾 500 行
      </Typography.Paragraph>
      <Spin spinning={loading && !logs}>
        <div className="log-viewer" ref={logRef} onScroll={handleLogScroll}>
          <pre>{logs?.logs || (logs?.pod_name ? '当前 Pod 暂无日志输出' : '任务尚未分配训练 Pod，日志将在 Pod 创建后显示')}</pre>
        </div>
      </Spin>
    </Card>
  );
}
