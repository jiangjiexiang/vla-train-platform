import { Typography } from 'antd';

import type { PreflightCheck } from '../../types/api';

/** 预检结果：按 level 分色，逐条给出 message 与可执行的 suggestion。 */
export function PreflightList({ checks }: { checks: PreflightCheck[] }) {
  if (checks.length === 0) return null;
  return (
    <ul className="preflight-list" data-testid="preflight-list">
      {checks.map((check, index) => (
        <li key={`${check.code}-${index}`} className={`dataset-issue dataset-issue-${check.level}`}>
          <div>
            <Typography.Text
              strong
              type={check.level === 'error' ? 'danger' : check.level === 'warning' ? 'warning' : undefined}
            >
              {check.message}
            </Typography.Text>
            <Typography.Text type="secondary" className="preflight-suggestion">
              {check.suggestion}
            </Typography.Text>
          </div>
        </li>
      ))}
    </ul>
  );
}
