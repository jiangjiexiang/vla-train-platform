import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { ApiOutlined, LockOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { getErrorMessage } from '../api/client';
import { useAuth } from '../context/AuthContext';

interface LoginForm {
  token: string;
}

export function LoginPage() {
  const { authRequired, isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (authRequired === null) return null;
  if (isAuthenticated) return <Navigate to="/dashboard" replace />;

  const submit = async ({ token: value }: LoginForm) => {
    setLoading(true);
    setError(null);
    try {
      await login(value);
      const from = (location.state as { from?: string } | null)?.from || '/dashboard';
      navigate(from, { replace: true });
    } catch (reason) {
      setError(getErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login-page">
      <Card className="login-card">
        <div className="login-brand">
          <span className="login-logo"><ApiOutlined /></span>
          <Typography.Title level={2}>VLA 训练平台</Typography.Title>
          <Typography.Paragraph type="secondary">GPU 训练任务提交、排队与日志管理</Typography.Paragraph>
        </div>
        {error && <Alert type="error" showIcon message="登录失败" description={error} className="login-error" />}
        <Form<LoginForm> layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item
            name="token"
            label="访问令牌"
            rules={[{ required: true, whitespace: true, message: '请输入访问令牌' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="请输入平台 Bearer Token"
              autoComplete="current-password"
              size="large"
              autoFocus
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={loading}>
            验证并登录
          </Button>
        </Form>
        <Typography.Paragraph type="secondary" className="login-help">
          令牌仅保存在当前浏览器中，请求时通过 Authorization: Bearer 头发送。
        </Typography.Paragraph>
      </Card>
    </main>
  );
}
