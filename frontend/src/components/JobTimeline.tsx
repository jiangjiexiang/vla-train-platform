import { Card, Empty, Timeline, Typography } from 'antd';
import type { JobTimelineResponse, TimelineStageState } from '../types/api';

/**
 * 作业阶段时间轴。
 *
 * 用户最困惑的是「提交之后到底发生了什么」。后端把 Workload、Job、Pod 的状态
 * 拼成一条流水线，这里只负责呈现：已完成的打勾、当前阶段高亮、未到达的置灰、
 * 失败标红。取不到时间戳的阶段显示「—」——不要编造时间。
 */
const STATE_COLOR: Record<TimelineStageState, string> = {
  done: 'green',
  current: 'blue',
  pending: 'gray',
  failed: 'red',
};

const STATE_TEXT: Record<TimelineStageState, string> = {
  done: '已完成',
  current: '进行中',
  pending: '未开始',
  failed: '失败',
};

function formatAt(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function JobTimeline({ data }: { data: JobTimelineResponse | null }) {
  if (!data || data.items.length === 0) {
    return (
      <Card title="阶段进度" className="section-card" data-testid="job-timeline">
        <Empty description="暂无法获取阶段信息" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    );
  }

  return (
    <Card title="阶段进度" className="section-card" data-testid="job-timeline">
      <Timeline
        items={data.items.map((item) => ({
          color: STATE_COLOR[item.state] ?? 'gray',
          children: (
            <div>
              <Typography.Text
                strong={item.state === 'current'}
                type={item.state === 'pending' ? 'secondary' : undefined}
              >
                {item.label}
              </Typography.Text>
              <Typography.Text type="secondary" className="timeline-state">
                {` · ${STATE_TEXT[item.state] ?? item.state}`}
              </Typography.Text>
              <div>
                <Typography.Text type="secondary" className="timeline-at">
                  {formatAt(item.at)}
                </Typography.Text>
              </div>
            </div>
          ),
        }))}
      />
    </Card>
  );
}
