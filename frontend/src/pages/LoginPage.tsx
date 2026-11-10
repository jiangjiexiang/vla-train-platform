import { Alert, Button, Card, Form, Input, Segmented, Typography } from 'antd';
import { ApiOutlined, LockOutlined, UserOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { getErrorMessage } from '../api/client';
import { useAuth } from '../context/AuthContext';
import type { AuthMode } from '../types/api';

interface TokenForm {
  token: string;
}

interface PasswordForm {
  username: string;
  password: string;
}

type LoginTab = Exclude<AuthMode, 'off'>;

// 区分「认证服务不可用」（503 / 网络异常）与「凭证错误」，避免把 LDAP 故障误报成密码错
export function classifyLoginError(error: string | null): string {
  if (!error) return '登录失败';
  if (/认证服务不可用|无法连接 API 服务/.test(error)) return '认证服务不可用';
  return '登录失败';
}

// 登录失败文案：503 单独换成「认证服务不可用」，其余沿用后端 detail
export function getLoginErrorMessage(reason: unknown): string {
  const status = (reason as { status?: unknown } | null)?.status;
  if (status === 503) return '认证服务不可用（LDAP 暂时无法连接），请稍后重试或联系管理员';
  return getErrorMessage(reason);
}

export function LoginPage() {
  const { authRequired, authMode, isAuthenticated, message, login, loginWithToken } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [tab, setTab] = useState<LoginTab | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 匿名（off）模式：不需要登录，直接进入首页（只读健康检查结果，不依赖令牌状态）
  if (authRequired === false) return <Navigate to="/dashboard" replace />;
  if (authRequired === null) return null;
  if (isAuthenticated) return <Navigate to="/dashboard" replace />;

  // 默认模式由 /health 的 auth_mode 决定；用户仍可手动切换（兼容后端尚未部署 /auth/login 的情况）
  // local 和 ldap 都是「用户名 + 密码」，默认就落到那个 tab。
  // 早先的判断只认 ldap，于是 local 模式默认停在「令牌登录」——
  // 用户看到的是一个要粘贴 64 位令牌的框，账号密码根本没地方输，
  // 只能自己去点第二个 tab。这就是「账号密码登不上」的直接原因。
  const defaultTab: LoginTab = authMode === 'local' || authMode === 'ldap' ? 'ldap' : 'token';
  const activeTab: LoginTab = tab ?? defaultTab;

  const goNext = () => {
    const from = (location.state as { from?: string } | null)?.from || '/dashboard';
    navigate(from === '/login' ? '/dashboard' : from, { replace: true });
  };

  const submitToken = async ({ token }: TokenForm) => {
    setLoading(true);
    setError(null);
    try {
      await loginWithToken(token);
      goNext();
    } catch (reason) {
      setError(getErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  const submitPassword = async ({ username, password }: PasswordForm) => {
    setLoading(true);
    setError(null);
    try {
      await login(username, password);
      goNext();
    } catch (reason) {
      setError(getLoginErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  // 401 失效跳转与健康检查失败都会写 message，优先展示本次提交的错误
  const alertMessage = error || message;
  const errorTitle = classifyLoginError(error);

  return (
    <main className="login-page">
      <Card className="login-card">
        <div className="login-brand">
          <span className="login-logo"><ApiOutlined /></span>
          <Typography.Title level={2}>训练平台</Typography.Title>
          <Typography.Paragraph type="secondary">GPU 训练任务提交、排队与日志管理</Typography.Paragraph>
        </div>
        {alertMessage && (
          <Alert
            type="error"
            showIcon
            message={errorTitle}
            description={alertMessage}
            className="login-error"
          />
        )}
        {authMode === 'ldap' && (
          <Alert
            type="info"
            showIcon
            className="login-error"
            message="本平台使用公司 LDAP/AD 账号登录，请输入域账号的用户名与密码。"
          />
        )}
        {authMode === 'local' && (
          <Alert
            type="info"
            showIcon
            className="login-error"
            message="请输入平台账号与密码。忘记密码请联系管理员重置。"
          />
        )}
        <Segmented<LoginTab>
          block
          className="login-mode-switch"
          value={activeTab}
          onChange={(value) => {
            setTab(value);
            setError(null);
          }}
          options={[
            { value: 'token', label: '令牌登录' },
            { value: 'ldap', label: '账号密码登录' },
          ]}
        />
        {activeTab === 'ldap' ? (
          <Form<PasswordForm> layout="vertical" onFinish={submitPassword} requiredMark={false}>
            <Form.Item
              name="username"
              label={<span className="plain-label">用户名</span>}
              rules={[{ required: true, whitespace: true, message: '请输入用户名' }]}
            >
              <Input
                prefix={<UserOutlined />}
                placeholder={authMode === 'local' ? '请输入用户名' : '请输入 LDAP/AD 用户名'}
                autoComplete="username"
                size="large"
                autoFocus
              />
            </Form.Item>
            <Form.Item
              name="password"
              label={<span className="plain-label">密码</span>}
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                placeholder={authMode === 'local' ? '请输入密码' : '请输入 LDAP/AD 密码'}
                autoComplete="current-password"
                size="large"
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" size="large" block loading={loading}>
              登录
            </Button>
          </Form>
        ) : (
          <Form<TokenForm> layout="vertical" onFinish={submitToken} requiredMark={false}>
            <Form.Item
              name="token"
              label={<span className="plain-label">访问令牌</span>}
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
        )}
        <Typography.Paragraph type="secondary" className="login-help">
          {activeTab === 'ldap'
            ? '密码只在登录时发送给平台校验，平台据此签发短期会话令牌，不会保存密码。'
            : '令牌仅保存在当前浏览器中，请求时通过 Authorization: Bearer 头发送。'}
        </Typography.Paragraph>
      </Card>
    </main>
  );
}