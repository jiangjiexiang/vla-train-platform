import { Alert, Button } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';

interface ErrorAlertProps {
  error: string | null;
  onRetry?: () => void;
  title?: string;
}

export function ErrorAlert({ error, onRetry, title = '数据加载失败' }: ErrorAlertProps) {
  if (!error) return null;
  return (
    <Alert
      type="error"
      showIcon
      message={title}
      description={error}
      action={onRetry ? (
        <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>重试</Button>
      ) : undefined}
      className="error-alert"
    />
  );
}
