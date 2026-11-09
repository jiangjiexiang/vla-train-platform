import { App as AntdApp, ConfigProvider, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import type { ReactNode } from 'react';
import { AppLayout } from './components/AppLayout';
import { ForbiddenPage } from './components/ForbiddenPage';
import { AuthProvider, useAuth } from './context/AuthContext';
import { DashboardPage } from './pages/DashboardPage';
import { JobDetailPage } from './pages/JobDetailPage';
import { JobListPage } from './pages/JobListPage';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { QueuesPage } from './pages/QueuesPage';
import { SubmitJobPage } from './pages/SubmitJobPage';
import { UsersPage } from './pages/UsersPage';

function LoadingScreen() {
  return <Spin size="large" style={{ display: 'block', margin: '160px auto' }} />;
}

function ProtectedRoutes() {
  const { authRequired, isAuthenticated, message } = useAuth();
  const location = useLocation();
  if (authRequired === null) return <LoadingScreen />;
  if (!isAuthenticated) {
    // 401 失效时把提示带到登录页，避免用户不知道为什么被踢回
    return <Navigate to="/login" replace state={{ from: location.pathname, message }} />;
  }
  return <AppLayout />;
}

// 管理员专属页面：非 admin 渲染无权限页，而不是报错或落到 404
function AdminOnly({ children }: { children: ReactNode }) {
  const { isAdmin } = useAuth();
  if (!isAdmin) return <ForbiddenPage />;
  return <>{children}</>;
}

function HomeRedirect() {
  const { authRequired, isAuthenticated } = useAuth();
  if (authRequired === null) return <LoadingScreen />;
  return <Navigate to={isAuthenticated ? '/dashboard' : '/login'} replace />;
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: { colorPrimary: '#315efb', borderRadius: 8, colorBgLayout: '#f4f6fa' },
        components: { Layout: { headerBg: '#101828', headerHeight: 64 } },
      }}
    >
      <AntdApp>
        <AuthProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route element={<ProtectedRoutes />}>
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/submit" element={<SubmitJobPage />} />
                <Route path="/jobs" element={<JobListPage />} />
                <Route path="/jobs/:name" element={<JobDetailPage />} />
                <Route path="/queues" element={<QueuesPage />} />
                <Route path="/users" element={<AdminOnly><UsersPage /></AdminOnly>} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
              <Route path="/" element={<HomeRedirect />} />
            </Routes>
          </BrowserRouter>
        </AuthProvider>
      </AntdApp>
    </ConfigProvider>
  );
}