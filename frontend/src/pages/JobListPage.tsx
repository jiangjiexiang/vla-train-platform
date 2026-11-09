import { Button, Card, Empty, Input, Select, Space, Table, Typography } from 'antd';
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { ColumnsType } from 'antd/es/table';
import { api, getErrorMessage, isAbortError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { JobActions } from '../components/JobActions';
import { PriorityTag, StatusTag } from '../components/StatusTag';
import { usePolling } from '../hooks/usePolling';
import type { Job, JobStatus } from '../types/api';
import { formatDateTime, formatDuration } from '../utils/format';

const statusOptions: Array<{ value: JobStatus | 'ALL'; label: string }> = [
  { value: 'ALL', label: '全部状态' },
  { value: 'QUEUED', label: '排队中' },
  { value: 'RUNNING', label: '运行中' },
  { value: 'SUCCEEDED', label: '已成功' },
  { value: 'FAILED', label: '已失败' },
  { value: 'CANCELLED', label: '已取消' },
  { value: 'UNKNOWN', label: '状态未知' },
];

export function JobListPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<JobStatus | 'ALL'>('ALL');
  const [keyword, setKeyword] = useState('');

  const refresh = usePolling(async (signal) => {
    try {
      const data = await api.getJobs(signal);
      setJobs(data.items);
      setError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setError(getErrorMessage(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  });

  const filteredJobs = useMemo(() => jobs.filter((job) => {
    const matchesStatus = status === 'ALL' || job.status === status;
    const query = keyword.trim().toLowerCase();
    const matchesKeyword = !query || job.name.toLowerCase().includes(query) || job.image.toLowerCase().includes(query);
    return matchesStatus && matchesKeyword;
  }), [jobs, status, keyword]);

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
    { title: 'GPU', dataIndex: 'gpus', key: 'gpus', width: 75, render: (value: number) => `${value} 卡` },
    { title: '优先级', dataIndex: 'priority', key: 'priority', width: 90, render: (value: string) => <PriorityTag priority={value} /> },
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

  return (
    <div>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>训练任务</Typography.Title>
          <Typography.Text type="secondary">查看实时与历史任务，取消运行或复制提交参数</Typography.Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/submit')}>提交任务</Button>
        </Space>
      </div>
      <ErrorAlert error={error} onRetry={refresh} title="任务列表加载失败" />
      <Card>
        <Space className="table-filters" wrap>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索任务名或镜像"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            className="job-search"
          />
          <Select value={status} options={statusOptions} onChange={setStatus} className="status-filter" />
        </Space>
        <Table<Job>
          rowKey="name"
          columns={columns}
          dataSource={filteredJobs}
          loading={loading && jobs.length === 0}
          scroll={{ x: 1320 }}
          locale={{ emptyText: <Empty description={jobs.length ? '没有符合筛选条件的任务' : '暂无训练任务，点击“提交任务”开始'} /> }}
          pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (total) => `共 ${total} 个任务` }}
        />
      </Card>
    </div>
  );
}
