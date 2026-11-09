import { Avatar, Button, Dropdown, Layout, Menu, Space, Tag, Typography } from 'antd';
import {
  ApiOutlined,
  DashboardOutlined,
  LogoutOutlined,
  PlusOutlined,
  ProfileOutlined,
  TeamOutlined,
  UnorderedListOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { API_BASE } from '../api/client';
import { useAuth } from '../context/AuthContext';

const { Header, Content, Footer } = Layout;

const ROLE_LABELS: Record<string, string> = { admin: '管理员', user: '普通用户' };
const SOURCE_LABELS: Record<string, string> = { off: '匿名', token: '令牌', ldap: 'LDAP' };

export function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { authRequired, authMode, user, isAdmin, logout } = useAuth();
  const selectedKey = location.pathname.startsWith('/jobs') ? '/jobs' : location.pathname;

    // 用户与配额管理仅 admin 可见（路由层也会再拦一次）
  const menuItems: MenuProps['items'] = [
    { key: '/dashboard', icon: <DashboardOutlined />, label: '集群总览' },
    { key: '/submit', icon: <PlusOutlined />, label: '提交任务' },
    { key: '/jobs', icon: <ProfileOutlined />, label: '训练任务' },
    { key: '/queues', icon: <UnorderedListOutlined />, label: '资源队列' },
    ...(isAdmin ? [{ key: '/users', icon: <TeamOutlined />, label: '用户与配额' }] : []),
  ];

    const handleLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  const userMenu: MenuProps['items'] = [
    {
      key: 'identity',
      disabled: true,
      label: (
        <div className="identity-summary">
          <div>{user?.username || '未知用户'}</div>
          <Typography.Text type="secondary">
            {`${ROLE_LABELS[user?.role ?? ''] || user?.role || '—'} · ${SOURCE_LABELS[user?.source ?? ''] || user?.source || '—'}`}
          </Typography.Text>
        </div>
      ),
    },
    { type: 'divider' },
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', onClick: () => void handleLogout() },
  ];

  return (
    <Layout className="app-shell">
      <Header className="app-header">
        <div className="brand" onClick={() => navigate('/dashboard')} role="button" tabIndex={0}>
          <span className="brand-mark"><ApiOutlined /></span>
          <span>高性能分布系统</span>
        </div>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          className="main-menu"
        />
        <Space size={8} className="header-user">
          {user && (
            <>
              <Tag color={isAdmin ? 'blue' : 'default'} className="role-tag">
                {ROLE_LABELS[user.role] || user.role}
              </Tag>
              <Dropdown menu={{ items: userMenu }} placement="bottomRight" trigger={['click']}>
                <Button type="text" className="user-button" aria-label="当前用户菜单">
                  <Avatar size="small" icon={<UserOutlined />} />
                  <span className="user-name">{user.username}</span>
                </Button>
              </Dropdown>
            </>
          )}
          {authRequired && (
            <Button type="text" className="logout-button" icon={<LogoutOutlined />} onClick={() => void handleLogout()}>
              退出
            </Button>
          )}
        </Space>
      </Header>
      <Content className="app-content">
        <Outlet />
      </Content>
      <Footer className="app-footer">
        <Space split={<Typography.Text type="secondary">·</Typography.Text>}>
          <span>GPU 训练任务排队与调度</span>
          <Typography.Text type="secondary">API: {API_BASE}</Typography.Text>
          <Typography.Text type="secondary">鉴权模式：{authMode === 'ldap' ? 'LDAP' : authMode === 'token' ? '访问令牌' : '免登录'}</Typography.Text>
          <Typography.Text type="secondary">数据每 4 秒自动刷新</Typography.Text>
        </Space>
      </Footer>
    </Layout>
  );
}