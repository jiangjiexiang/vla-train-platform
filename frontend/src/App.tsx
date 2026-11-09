import { App as AntdApp, ConfigProvider, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppLayout } from './components/AppLayout';
import { AuthProvider, useAuth } from './context/AuthContext';
import { DashboardPage } from './pages/DashboardPage';
import { JobDetailPage } from './pages/JobDetailPage';
import { JobListPage } from './pages/JobListPage';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { QueuesPage } from './pages/QueuesPage';
import { SubmitJobPage } from './pages/SubmitJobPage';

function ProtectedRoutes() {
  const { authRequired, isAuthenticated } = useAuth();
  const location = useLocation();
  if (authRequired === null) return <Spin size="large" style={{ display: 'block', margin: '160px auto' }} />;
  if (!isAuthenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <AppLayout />;
}

function HomeRedirect() {
  const { authRequired, isAuthenticated } = useAuth();
  if (authRequired === null) return <Spin size="large" style={{ display: 'block', margin: '160px auto' }} />;
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
