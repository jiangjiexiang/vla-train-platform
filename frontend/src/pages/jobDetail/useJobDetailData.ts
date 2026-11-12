import { useState } from 'react';
import { api, getErrorMessage, isAbortError } from '../../api/client';
import { usePolling } from '../../hooks/usePolling';
import type {
  Job,
  JobCheckpointsResponse,
  JobDiagnosisResponse,
  JobEventsResponse,
  JobLogs,
  JobMetricsResponse,
  JobTimelineResponse,
} from '../../types/api';

export function useJobDetailData(name: string) {
  const [job, setJob] = useState<Job | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [jobLoading, setJobLoading] = useState(true);
  const [logs, setLogs] = useState<JobLogs | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(true);
  const [events, setEvents] = useState<JobEventsResponse | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [metrics, setMetrics] = useState<JobMetricsResponse | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [diagnosis, setDiagnosis] = useState<JobDiagnosisResponse | null>(null);
  const [diagnosisError, setDiagnosisError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<JobTimelineResponse | null>(null);
  const [checkpoints, setCheckpoints] = useState<JobCheckpointsResponse | null>(null);

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

  const refreshMetrics = usePolling(async (signal) => {
    try {
      const data = await api.getJobMetrics(name, 500, signal);
      setMetrics(data);
      setMetricsError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setMetricsError(getErrorMessage(reason));
    }
  }, 10000);

  const refreshDiagnosis = usePolling(async (signal) => {
    try {
      const data = await api.getJobDiagnosis(name, signal);
      setDiagnosis(data);
      setDiagnosisError(null);
    } catch (reason) {
      if (!isAbortError(reason)) setDiagnosisError(getErrorMessage(reason));
    }
  }, 30000);

  usePolling(async (signal) => {
    try {
      setTimeline(await api.getJobTimeline(name, signal));
    } catch (reason) {
      if (!isAbortError(reason)) setTimeline(null);
    }
  }, 5000);

  usePolling(async (signal) => {
    try {
      setCheckpoints(await api.getJobCheckpoints(name, signal));
    } catch (reason) {
      if (!isAbortError(reason)) setCheckpoints(null);
    }
  }, 10000);

  const refreshAll = () => {
    refreshJob();
    refreshLogs();
    refreshEvents();
    refreshMetrics();
    refreshDiagnosis();
  };

  return {
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
  };
}
