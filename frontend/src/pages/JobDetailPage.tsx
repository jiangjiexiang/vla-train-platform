import { Button, Empty, Space, Spin, Typography } from 'antd';
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { Link, useParams } from 'react-router-dom';
import { ErrorAlert } from '../components/ErrorAlert';
import { JobActions } from '../components/JobActions';
import { StatusTag } from '../components/StatusTag';
import { JobEventsCard, JobLogsCard, JobOverview, TrainingMetricsCard } from './jobDetail/JobDetailSections';
import { useJobDetailData } from './jobDetail/useJobDetailData';

export function JobDetailPage() {
  const { name = '' } = useParams();
  const {
    job,
    jobError,
    jobLoading,
    logs,
    logError,
    logsLoading,
    events,
    eventsError,
    eventsLoading,
    metrics,
    metricsError,
    diagnosis,
    diagnosisError,
    timeline,
    checkpoints,
    refreshJob,
    refreshLogs,
    refreshEvents,
    refreshMetrics,
    refreshDiagnosis,
    refreshAll,
  } = useJobDetailData(name);

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
          <JobOverview
            job={job}
            diagnosis={diagnosis}
            diagnosisError={diagnosisError}
            onRetryDiagnosis={refreshDiagnosis}
            timeline={timeline}
            checkpoints={checkpoints}
          />
        ) : !jobLoading && !jobError ? <Empty description="未找到任务详情" /> : null}
      </Spin>

      <TrainingMetricsCard
        jobName={name}
        metrics={metrics}
        metricsError={metricsError}
        onRetry={refreshMetrics}
      />
      <JobEventsCard
        events={events}
        error={eventsError}
        loading={eventsLoading}
        onRetry={refreshEvents}
      />
      <JobLogsCard
        logs={logs}
        error={logError}
        loading={logsLoading}
        onRetry={refreshLogs}
      />
    </div>
  );
}
