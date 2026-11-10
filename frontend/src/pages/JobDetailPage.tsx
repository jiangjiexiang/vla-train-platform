import { Alert, Button, Card, Col, Collapse, Descriptions, Empty, List, Row, Segmented, Space, Spin, Tag, Typography } from 'antd';
import { ArrowDownOutlined, ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { JobActions } from '../components/JobActions';
import { CheckpointList } from '../components/CheckpointList';
import { JobTimeline } from '../components/JobTimeline';
import { LossChart, downsample } from '../components/LossChart';
import { PriorityTag, StatusTag } from '../components/StatusTag';
import { usePolling } from '../hooks/usePolling';
import type {
  Job,
  JobDiagnosisResponse,
  JobEventsResponse,
  JobLogs,
  JobMetricsResponse,
  JobTimelineResponse,
  JobCheckpointsResponse,
  MetricPoint,
  TensorboardResponse,
} from '../types/api';
import { formatDateTime, formatDuration, formatQueueEta } from '../utils/format';


/** 从真实指标点里推断哪些字段可以画。

 * 不写死列表：不同训练脚本写的字段不同（冒烟脚本只有 loss，
 * SmolVLA 还写 grad_norm/lr/update_s），按数据推断才不会出现空标签页。
 */
const METRIC_LABELS: Record<string, string> = {
  loss: 'loss',
  grad_norm: '梯度范数',
  lr: '学习率',
  update_s: '每步耗时',
  // 第三方脚本常带 train/ 前缀，与裸键值相同，所以标签重复，靠 value（tag 本身）区分
  'train/loss': 'loss',
  'train/grad_norm': '梯度范数',
  'train/lr': '学习率',
  'train/update_s': '每步耗时',
};

// loss 永远排第一，其余按固定顺序补齐
/**
 * TensorBoard 不轮询：给一个足够大的间隔，等价于「只加载一次」。
 *
 * 注意 usePolling 的第二个参数是间隔、第三个才是 enabled。
 * 这里如果传 0，setTimeout(run, 0) 等于「立刻再跑一次」，
 * 切到 TensorBoard 标签后会疯狂重请求（实测 4 秒 55 次）。
 */
const TENSORBOARD_IDLE_INTERVAL = 24 * 60 * 60 * 1000;

const METRIC_ORDER = ['loss', 'grad_norm', 'lr', 'update_s', 'train/loss', 'train/grad_norm', 'train/lr', 'train/update_s'];

function metricOptions(items: MetricPoint[]): Array<{ label: string; value: string }> {
  const keys = new Set<string>();
  for (const point of items) {
    for (const [key, value] of Object.entries(point)) {
      if (key === 'step') continue;
      if (typeof value === 'number' && Number.isFinite(value)) keys.add(key);
    }
  }
  return METRIC_ORDER
    .filter((key) => keys.has(key))
    .map((key) => ({ label: METRIC_LABELS[key] ?? key, value: key }));
}

/** TensorBoard 的 tag 切换器。

 * 后端只返回真正有数据的 tag，但空数组的 tag 仍可能出现（events 文件存在、
 * 某个 tag 一个点都没写），这类 tag 画不出来，直接不给入口，避免点进去看到空图。
 * 裸键与 train/* 同时存在时两个入口都会保留：名称看起来一样，但数据来源不同，
 * 后端返回的 tag 列表就是事实，前端不替用户做取舍。
 */
function tensorboardOptions(data: TensorboardResponse): Array<{ label: string; value: string }> {
  const tags = data.tags.filter((tag) => (data.series[tag]?.length ?? 0) > 0);
  const order = (tag: string) => {
    const index = METRIC_ORDER.indexOf(tag);
    return index === -1 ? METRIC_ORDER.length : index;
  };
  return [...tags]
    .sort((a, b) => order(a) - order(b) || a.localeCompare(b))
    .map((tag) => ({ label: METRIC_LABELS[tag] ?? tag, value: tag }));
}

/** TensorBoard 区块。

 * 独立成组件而不是塞进页面：页面里已经有 job/logs/events/metrics/diagnosis 五路轮询，
 * 再多一段 JSX 会让本来就长的 render 更难读。
 */
function TensorboardPanel({
  data,
  error,
  tag,
  onTagChange,
  onRetry,
}: {
  data: TensorboardResponse | null;
  error: string | null;
  tag: string | null;
  onTagChange: (tag: string) => void;
  onRetry: () => void;
}) {
  const options = data ? tensorboardOptions(data) : [];
  const activeTag = tag ?? options[0]?.value ?? null;
  const points = data && activeTag ? data.series[activeTag] ?? [] : [];

  return (
    <div data-testid="tensorboard-panel">
      <ErrorAlert error={error} onRetry={onRetry} title="TensorBoard 数据加载失败" />
      {error ? null : data === null ? (
        <div className="event-placeholder" />
      ) : !data.path_exists ? (
        // 老任务、老镜像都没有 events 文件。只说「暂无数据」用户会以为平台坏了，
        // 所以要说清这是镜像版本问题，以及怎么才能看到。
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          data-testid="tensorboard-empty"
          description={(
            <Space direction="vertical" size={4}>
              <Typography.Text>
                该任务还没生成 TensorBoard 数据。用新镜像（2026-11-10 之后构建的 vla-smolvla）提交的任务才会有。
              </Typography.Text>
              <Typography.Text type="secondary">{`扫描目录：${data.dir}`}</Typography.Text>
            </Space>
          )}
        />
      ) : options.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          data-testid="tensorboard-empty"
          description={`${data.dir} 里有 events 文件，但其中没有平台支持的标量（loss / grad_norm / lr / update_s）。`}
        />
      ) : (
        <div>
          <Space size={8} wrap>
            {options.length > 1 && (
              <Segmented
                size="small"
                value={activeTag ?? undefined}
                onChange={(value) => onTagChange(String(value))}
                options={options}
                data-testid="tensorboard-tag-switch"
              />
            )}
            <Tag color="blue" data-testid="tensorboard-point-count">
              {`${points.length} 个点`}
            </Tag>
            {data.truncated && (
              // 抽稀后的点数和真实步数对不上，不说明的话会有人拿「点数」当「训练步数」
              <Tag color="orange" data-testid="tensorboard-truncated">数据点较多，已抽稀显示</Tag>
            )}
          </Space>
          {/*
            LossChart 的入参是 MetricPoint[]（按 metric 字段取值），
            而 TensorBoard 的 series[tag] 是 {step, value}。
            在调用处把 value 映射成当前 tag 字段名即可，不需要为这点差异改动 LossChart 的公共接口；
            在调用处转换即可，不需要为这点差异改动 LossChart 的公共接口。
          */}
          <LossChart
            points={downsample(points).map((point) => ({ step: point.step, [activeTag as string]: point.value }))}
            metric={activeTag ?? 'loss'}
          />
          {data.truncated && (
            <Typography.Text type="secondary" className="hint">
              后端最多返回 2000 个点，超出部分按等间隔抽稀（保留首尾），曲线形状仍然可参考；需要精确数值请下载 events 文件用 TensorBoard 查看。
            </Typography.Text>
          )}
        </div>
      )}
    </div>
  );
}

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
  const [metrics, setMetrics] = useState<JobMetricsResponse | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [diagnosis, setDiagnosis] = useState<JobDiagnosisResponse | null>(null);
  const [diagnosisError, setDiagnosisError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<JobTimelineResponse | null>(null);
  const [checkpoints, setCheckpoints] = useState<JobCheckpointsResponse | null>(null);
  // 训练脚本写出的指标不止 loss：还有 grad_norm、lr、update_s 等。
  // 用户排查训练问题时最常看的是 grad_norm 有没有爆炸，
  // 只看 loss 会漏掉「loss 平稳但梯度已经发散」这类情况。
  const [metricKey, setMetricKey] = useState('loss');
  // 训练曲线数据源。默认仍是平台指标（metrics.jsonl）：它随任务实时轮询，
  // 老任务也都有；TensorBoard 只在用户主动切过去时才请求（见下面 usePolling 的 enabled）
  const [metricSource, setMetricSource] = useState<'platform' | 'tensorboard'>('platform');
  const [tensorboard, setTensorboard] = useState<TensorboardResponse | null>(null);
  const [tensorboardError, setTensorboardError] = useState<string | null>(null);
  // TensorBoard 的 tag 与 metrics.jsonl 的字段名不完全重合（还可能是 train/*），
  // 两边共用 metricKey 会切出「当前 tag 不存在」的空图，所以各记一份
  const [tensorboardTag, setTensorboardTag] = useState<string | null>(null);
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

  // 训练中随轮询自动刷新曲线；任务结束后指标文件不再变化，用 15 秒兜底
  const refreshMetrics = usePolling(async (signal) => {
    try {
      const data = await api.getJobMetrics(name, 500, signal);
      setMetrics(data);
      setMetricsError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setMetricsError(getErrorMessage(reason));
    }
  }, 10000);

  // TensorBoard 按需加载：解析 events 文件要读宿主机上的二进制文件，
  // 比读 metrics.jsonl 重得多。绝大多数用户只看平台指标，
  // 如果在详情页加载时无条件请求，会把首屏拖慢且浪费一次解析。
  // 因此只在用户切到 TensorBoard 时才发请求；解析结果直接复用现有 LossChart 画图，
  // 这不改变后端「一个请求返回全部 tag」的接口形态，也不需要再引入一个前端图表库。
  // 不轮询：events 文件由训练进程追加写，TensorBoard 是「查历史」而不是盯实时，
  // 用户要最新数据时可以点右上角「刷新」（refreshAll 同理会触发）。
  const refreshTensorboard = usePolling(async (signal) => {
    try {
      const data = await api.getJobTensorboard(name, 2000, signal);
      setTensorboard(data);
      setTensorboardError(null);
      // 默认落到第一个可画的 tag，并保证当前选中的 tag 仍然存在
      setTensorboardTag((current) => {
        const usable = data.tags.filter((tag) => (data.series[tag]?.length ?? 0) > 0);
        return current && usable.includes(current) ? current : usable[0] ?? null;
      });
    } catch (reason) {
      if (!isAbortError(reason)) setTensorboardError(getErrorMessage(reason));
    }
    // 第三个参数是 enabled，第二个是轮询间隔。
    // 之前这里传的是 0，usePolling 里 `setTimeout(run, 0)` 等于「立刻再跑一次」，
    // 于是只要切到这个标签就会疯狂重请求（实测 4 秒 55 次）。
    // TensorBoard 读的是历史 events，本来就不该轮询，
    // 给一个很大的间隔当「不轮询」用，真正的新数据由右上角刷新触发。
  }, TENSORBOARD_IDLE_INTERVAL, metricSource === 'tensorboard');

  // 诊断只在任务失败时有内容，接口本身在未失败时返回 diagnosis: null
  const refreshDiagnosis = usePolling(async (signal) => {
    try {
      const data = await api.getJobDiagnosis(name, signal);
      setDiagnosis(data);
      setDiagnosisError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setDiagnosisError(getErrorMessage(reason));
    }
  }, 30000);

  // 时间轴随任务推进而变，5 秒刷新一次即可（阶段切换不像日志那样频繁）
  usePolling(async (signal) => {
    try {
      setTimeline(await api.getJobTimeline(name, signal));
    } catch (reason) {
      // 时间轴不是关键信息，拿不到就不显示，不打扰用户
      if (!isAbortError(reason)) setTimeline(null);
    }
  }, 5000);

  // 产物随训练推进陆续写入，10 秒刷新一次即可
  usePolling(async (signal) => {
    try {
      setCheckpoints(await api.getJobCheckpoints(name, signal));
    } catch (reason) {
      if (!isAbortError(reason)) setCheckpoints(null);
    }
  }, 10000);

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
    refreshMetrics();
    // 只在 TensorBoard 标签下才真的刷新它，否则「刷新」会顺手解析一次 events，
    // 又回到「不切标签也会请求」的老问题
    if (metricSource === 'tensorboard') refreshTensorboard();
    refreshDiagnosis();
  };

  // 排队 ETA 只在这一个地方算，页面上多处引用同一结果，避免重复调用与不一致
  const etaInfo = formatQueueEta(job?.queue_eta_seconds ?? null, job?.queue_eta_source ?? null);

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
              <ErrorAlert error={diagnosisError} onRetry={refreshDiagnosis} title="失败诊断加载失败" />
            )}
            {job.queue_state === 'queued' && job.queue_position !== null && job.queue_position !== undefined && etaInfo && (
              <Alert
                className="job-message"
                type="info"
                showIcon
                // queue_position 的语义是「自己在队列中的位置」，队首为 1，
                // 所以它前面的任务数是 position - 1。两种说法都显示，避免读者误解。
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
        ) : !jobLoading && !jobError ? <Empty description="未找到任务详情" /> : null}
      </Spin>

      <Card
        title="训练 Loss 曲线"
        className="section-card"
        extra={(
          <Segmented
            size="small"
            value={metricSource}
            onChange={(value) => setMetricSource(value === 'tensorboard' ? 'tensorboard' : 'platform')}
            options={[
              { label: '平台指标', value: 'platform' },
              { label: 'TensorBoard', value: 'tensorboard' },
            ]}
            data-testid="metric-source-switch"
          />
        )}
      >
        {metricSource === 'platform' ? (
          <>
            <ErrorAlert error={metricsError} onRetry={refreshMetrics} title="训练指标加载失败" />
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
                  // 冒烟测试的配方默认只跑 5 步，曲线自然只有几个点。
                  // 不解释的话，用户看到稀疏的曲线会以为训练出了问题。
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
          <TensorboardPanel
            data={tensorboard}
            error={tensorboardError}
            tag={tensorboardTag}
            onTagChange={setTensorboardTag}
            onRetry={refreshTensorboard}
          />
        )}
      </Card>

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
