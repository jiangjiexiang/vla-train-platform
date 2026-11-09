import { App, Button, Card, Empty, Input, Select, Space, Table, Tag, Typography } from 'antd';
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { loadQuota } from '../api/quota';
import type { JobListParams } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { JobActions } from '../components/JobActions';
import { PriorityTag, StatusTag } from '../components/StatusTag';
import { usePolling } from '../hooks/usePolling';
import type { Job, JobStatus, UserQuota } from '../types/api';
import { formatDateTime, formatDuration } from '../utils/format';

const DEFAULT_PAGE_SIZE = 20;

// 后端仅支持这几种状态过滤；"全部" 表示不传 status
const statusOptions: Array<{ value: JobStatus | 'ALL'; label: string }> = [
  { value: 'ALL', label: '全部状态' },
  { value: 'QUEUED', label: '排队中' },
  { value: 'RUNNING', label: '运行中' },
  { value: 'SUCCEEDED', label: '已成功' },
  { value: 'FAILED', label: '已失败' },
  { value: 'CANCELLED', label: '已取消' },
];

export function JobListPage() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const [quota, setQuota] = useState<UserQuota | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<JobStatus | 'ALL'>('ALL');
  const [keyword, setKeyword] = useState('');
  // 防抖后的实际搜索词，避免每次按键都请求后端
  const [searchTerm, setSearchTerm] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // 配额来自 /auth/me；匿名模式、旧后端或未实现该接口时静默跳过，不影响列表
  useEffect(() => {
    const controller = new AbortController();
    loadQuota(controller.signal).then((value) => setQuota(value));
    return () => controller.abort();
  }, []);

  // 提交页配额超限时把原因带回来，这里只提示一次
  const quotaError = (location.state as { quotaError?: string } | null)?.quotaError;
  useEffect(() => {
    if (!quotaError) return;
    message.warning(quotaError);
    navigate(location.pathname, { replace: true });
  }, [quotaError, location.pathname, message, navigate]);

  // 输入停止 400ms 后才应用搜索词，同时回到第一页
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchTerm(keyword.trim());
      setPage(1);
    }, 400);
    return () => clearTimeout(timer);
  }, [keyword]);

  const params = useMemo<JobListParams>(() => {
    const next: JobListParams = {
      limit: pageSize,
      offset: (page - 1) * pageSize,
    };
    if (status !== 'ALL') next.status = status;
    if (searchTerm) next.q = searchTerm;
    return next;
  }, [page, pageSize, status, searchTerm]);

  const refresh = usePolling(async (signal) => {
    try {
      const data = await api.getJobs(params, signal);
      if (signal.aborted) return;
      setJobs(data.items);
      setTotal(data.total ?? data.items.length);
      setError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, 4000, true, JSON.stringify(params));

  const quotaInfo = describeQuota(jobs, quota);

  const columns: ColumnsType<Job> = [
    {
      title: '任务名称',
      dataIndex: 'name',
      key: 'name',
      fixed: 'left',
      width: 180,
      render: (name: string) => <Link to={`/jobs/${encodeURIComponent(name)}`}>{name}</Link>,
    },
    { title: '状态', dataIndex: 'status', key: 'status', width: 105, render: (value: JobStatus) => <StatusTag status={value} /> },
    {
      title: 'GPU',
      dataIndex: 'gpus',
      key: 'gpus',
      width: 95,
      render: (value: number, job) => {
        const nodes = job.nodes ?? 1;
        if (nodes <= 1) return `${value} 卡`;
        return (
          <span>
            {`${value} 卡`}
            <br />
            <Typography.Text type="secondary">{`${nodes} 节点`}</Typography.Text>
          </span>
        );
      },
    },
    { title: '优先级', dataIndex: 'priority', key: 'priority', width: 90, render: (value: string) => <PriorityTag priority={value} /> },
    {
      title: '提交者',
      dataIndex: 'owner',
      key: 'owner',
      width: 120,
      render: (value: string | undefined) => (value ? <Tag>{value}</Tag> : <Typography.Text type="secondary">—</Typography.Text>),
    },
    { title: '镜像', dataIndex: 'image', key: 'image', ellipsis: true, width: 210 },
    { title: 'Kueue / Pod', key: 'runtime', width: 170, render: (_, job) => <span>{job.workload_status || '—'} / {job.pod_status || '—'}</span> },
    { title: '运行时长', dataIndex: 'duration_seconds', key: 'duration', width: 130, render: formatDuration },
    { title: '提交时间', dataIndex: 'created_at', key: 'created_at', width: 180, render: formatDateTime },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 160,
      render: (_, job) => <JobActions job={job} onChanged={refresh} />,
    },
  ];

  const hasFilters = status !== 'ALL' || searchTerm !== '';
  const emptyDescription = hasFilters ? '没有匹配的任务，请调整筛选条件' : '还没有任务，去「快速开始」一键发起训练吧';

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>训练任务</Typography.Title>
          <Typography.Text type="secondary">查看实时与历史任务，取消运行或复制提交参数</Typography.Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/advanced/submit')}>提交任务</Button>
        </Space>
      </div>
      {quotaInfo.text && (
        <div
          className={`quota-banner${quotaInfo.exceeded ? ' quota-banner-exceeded' : ''}`}
          role="status"
          data-testid="quota-banner"
        >
          {quotaInfo.text}
        </div>
      )}
      <ErrorAlert error={error} onRetry={refresh} title="任务列表加载失败" />
      <Card>
        <Space className="table-filters" wrap>
          <Select
            value={status}
            options={statusOptions}
            onChange={(value: JobStatus | 'ALL') => {
              setStatus(value);
              setPage(1);
            }}
            className="status-filter"
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索任务名"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            className="job-search"
          />
        </Space>
        <Table<Job>
          rowKey="name"
          columns={columns}
          dataSource={jobs}
          loading={loading && jobs.length === 0}
          scroll={{ x: 1440 }}
          locale={{ emptyText: <Empty description={emptyDescription} /> }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (count) => `共 ${count} 个任务`,
            onChange: (nextPage, nextPageSize) => {
              if (nextPageSize !== pageSize) {
                setPageSize(nextPageSize);
                setPage(1);
              } else {
                setPage(nextPage);
              }
            },
          }}
        />
      </Card>
    </div>
  );
}

/**
 * 用当前页可见的排队中/运行中任务推算已用配额（契约 1.5 的计数口径：
 * 非终态任务的 gpus 之和与活跃任务数），与 /auth/me 返回的配额对比。
 */
export function describeQuota(
  jobs: Job[],
  quota: UserQuota | null,
): { text: string; gpuUsed: number; activeJobs: number; exceeded: boolean } {
  const empty = { text: '', gpuUsed: 0, activeJobs: 0, exceeded: false };
  if (!quota || (quota.max_gpus === null && quota.max_active_jobs === null)) return empty;

  const active = jobs.filter((job) => job.status === 'QUEUED' || job.status === 'RUNNING');
  const gpuUsed = active.reduce((sum, job) => sum + (job.gpus || 0), 0);
  const activeJobs = active.length;
  const parts: string[] = [];
  let exceeded = false;
  if (quota.max_gpus !== null) {
    if (gpuUsed > quota.max_gpus) exceeded = true;
    parts.push(`GPU 配额：已用 ${gpuUsed} / ${quota.max_gpus} 卡`);
  }
  if (quota.max_active_jobs !== null) {
    if (activeJobs > quota.max_active_jobs) exceeded = true;
    parts.push(`活跃任务：${activeJobs} / ${quota.max_active_jobs}`);
  }
  if (!parts.length) return empty;
  const note = '（按当前页的排队中/运行中任务统计）';
  const suffix = exceeded ? '，已超出配额，新任务将被拒绝' : '';
  return { text: `${parts.join(' · ')}${suffix}${note}`, gpuUsed, activeJobs, exceeded };
}