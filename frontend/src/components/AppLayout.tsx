import { Button, Layout, Menu, Space, Typography } from 'antd';
import {
  ApiOutlined,
  DashboardOutlined,
  LogoutOutlined,
  PlusOutlined,
  ProfileOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { API_BASE } from '../api/client';
import { useAuth } from '../context/AuthContext';

const { Header, Content, Footer } = Layout;

const menuItems = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '集群总览' },
  { key: '/submit', icon: <PlusOutlined />, label: '提交任务' },
  { key: '/jobs', icon: <ProfileOutlined />, label: '训练任务' },
  { key: '/queues', icon: <UnorderedListOutlined />, label: '资源队列' },
];

export function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { authRequired, logout } = useAuth();
  const selectedKey = location.pathname.startsWith('/jobs') ? '/jobs' : location.pathname;

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
        {authRequired && (
          <Button type="text" className="logout-button" icon={<LogoutOutlined />} onClick={logout}>
            退出
          </Button>
        )}
      </Header>
      <Content className="app-content">
        <Outlet />
      </Content>
      <Footer className="app-footer">
        <Space split={<Typography.Text type="secondary">·</Typography.Text>}>
          <span>GPU 训练任务排队与调度</span>
          <Typography.Text type="secondary">API: {API_BASE}</Typography.Text>
          <Typography.Text type="secondary">数据每 4 秒自动刷新</Typography.Text>
        </Space>
      </Footer>
    </Layout>
  );
}
