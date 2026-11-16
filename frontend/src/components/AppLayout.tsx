import { Avatar, Button, Dropdown, Layout, Menu, Space, Tag, Typography } from 'antd';
import {
  ApiOutlined,
  AppstoreOutlined,
  DashboardOutlined,
  EditOutlined,
  LogoutOutlined,
  ProfileOutlined,
  RocketOutlined,
  TeamOutlined,
  UnorderedListOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

const { Header, Content } = Layout;

const ROLE_LABELS: Record<string, string> = { admin: '管理员', user: '普通用户' };
const SOURCE_LABELS: Record<string, string> = { off: '匿名', token: '令牌', ldap: 'LDAP' };

export function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { authRequired, user, isAdmin, logout } = useAuth();
  // 子路径（/jobs/<name>、/advanced/submit）要选中对应的父菜单项
  const selectedKey = location.pathname.startsWith('/jobs')
    ? '/jobs'
    : location.pathname.startsWith('/advanced')
      ? '/advanced/submit'
      : location.pathname;

  // v1.0：默认路径是「快速开始」（一键训练），集群总览退到末尾
  // 用户与配额管理仅 admin 可见（路由层也会再拦一次）
  const menuItems: MenuProps['items'] = [
    { key: '/', icon: <RocketOutlined />, label: '快速开始' },
    { key: '/datasets', icon: <AppstoreOutlined />, label: '数据集' },
    { key: '/jobs', icon: <ProfileOutlined />, label: '任务' },
    { key: '/queues', icon: <UnorderedListOutlined />, label: '队列' },
    ...(isAdmin ? [{ key: '/users', icon: <TeamOutlined />, label: '用户与配额' }] : []),
    ...(isAdmin ? [{ key: '/admin/recipes', icon: <EditOutlined />, label: '配方管理' }] : []),
    { key: '/dashboard', icon: <DashboardOutlined />, label: '集群总览' },
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
        <div className="brand" onClick={() => navigate('/')} role="button" tabIndex={0}>
          <span className="brand-mark"><ApiOutlined /></span>
          <span>训练平台</span>
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
    </Layout>
  );
}