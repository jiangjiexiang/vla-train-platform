import { App, Button, Space } from 'antd';
import { CopyOutlined, StopOutlined } from '@ant-design/icons';
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
      {canCancel && (
        <Button type="link" danger size={size} icon={<StopOutlined />} onClick={confirmCancel}>
          取消
        </Button>
      )}
    </Space>
  );
}
