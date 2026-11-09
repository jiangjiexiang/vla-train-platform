import { Button, Result } from 'antd';
import { useNavigate } from 'react-router-dom';

export function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="请通过顶部导航访问平台功能。"
      extra={<Button type="primary" onClick={() => navigate('/')}>返回快速开始</Button>}
    />
  );
}
