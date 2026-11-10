import { App, Button, Space } from 'antd';
import { CopyOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { api, getErrorMessage } from '../api/client';
import type { Job } from '../types/api';

interface JobActionsProps {
  job: Job;
  onChanged?: () => void;
  size?: 'small' | 'middle';
}

export function JobActions({ job, onChanged, size = 'small' }: JobActionsProps) {
  const navigate = useNavigate();
  const { modal, message } = App.useApp();
  const canCancel = job.status === 'QUEUED' || job.status === 'RUNNING';

  const clone = () => {
    navigate('/advanced/submit', { state: { sourceJob: job } });
  };

  // 只有真的跑起来过的任务才谈得上续训：没开始过就没有 checkpoint。
  // 失败/取消的任务照样可以续——那正是最需要续训的场景，
  // 所以只看「跑过多久」，不看成功与否。
  const canResume =
    (job.status === 'SUCCEEDED' || job.status === 'FAILED' || job.status === 'CANCELLED') &&
    (job.duration_seconds ?? 0) > 0;

  const resume = () => {
    navigate('/advanced/submit', {
      state: { sourceJob: job, resumeFrom: job.name },
    });
  };

  const confirmCancel = () => {
    modal.confirm({
      title: `确认取消任务 ${job.name}？`,
      content: '任务将被删除并标记为已取消，占用的 GPU 会释放。此操作不会删除已保存的 checkpoint。',
      okText: '确认取消',
      cancelText: '返回',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await api.cancelJob(job.name);
          message.success(`任务 ${job.name} 已取消`);
          onChanged?.();
        } catch (error) {
          message.error(getErrorMessage(error));
          throw error;
        }
      },
    });
  };

  return (
    <Space size={4} onClick={(event) => event.stopPropagation()}>
      <Button type="link" size={size} icon={<CopyOutlined />} onClick={clone}>克隆</Button>
      {canResume && (
        <Button type="link" size={size} icon={<ReloadOutlined />} onClick={resume}>
          续训
        </Button>
      )}
      {canCancel && (
        <Button type="link" danger size={size} icon={<StopOutlined />} onClick={confirmCancel}>
          取消
        </Button>
      )}
    </Space>
  );
}
