import { Button, Result } from 'antd';
import { useNavigate } from 'react-router-dom';

interface ForbiddenPageProps {
  title?: string;
  subTitle?: string;
}

// 非管理员访问管理员页面时展示的「无权限」页，而不是抛错或落到 404
export function ForbiddenPage({
  title = '没有访问权限',
  subTitle = '该页面仅管理员可用。如需用户与配额管理权限，请联系平台管理员。',
}: ForbiddenPageProps) {
  const navigate = useNavigate();
  return (
    <Result
      status="403"
      title={title}
      subTitle={subTitle}
      extra={<Button type="primary" onClick={() => navigate('/dashboard')}>返回总览</Button>}
      className="forbidden-page"
    />
  );
}
