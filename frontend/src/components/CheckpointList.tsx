import { Card, Empty, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import type { CheckpointFile, JobCheckpointsResponse } from '../types/api';
import { formatDateTime } from '../utils/format';

/** 把字节数压成人类可读的大小。训练产物动辄几百 MB，原始字节数没法看。 */
export function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  // 小于 10 时保留一位小数（4.2 MB 比 4 MB 有用），否则取整
  return `${value < 10 && index > 0 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

const columns: ColumnsType<CheckpointFile> = [
  {
    title: '文件',
    dataIndex: 'name',
    key: 'name',
    render: (value: string, record) => (
      <Typography.Text code={record.is_weights} type={record.is_weights ? undefined : 'secondary'}>
        {value}
      </Typography.Text>
    ),
  },
  {
    title: '类型',
    dataIndex: 'is_weights',
    key: 'is_weights',
    width: 100,
    render: (isWeights: boolean) =>
      isWeights ? <Tag color="blue">模型权重</Tag> : <Tag>其它</Tag>,
  },
  {
    title: '大小',
    dataIndex: 'size_bytes',
    key: 'size_bytes',
    width: 110,
    align: 'right',
    render: (value: number) => formatBytes(value),
  },
  {
    title: '修改时间',
    dataIndex: 'modified_at',
    key: 'modified_at',
    width: 180,
    render: (value: string | null) => formatDateTime(value),
  },
];

/**
 * 训练产物清单。
 *
 * 任务跑完但没有产物，用户其实是白跑了一次。把产出的文件、大小、时间列出来，
 * 才能确认「这次训练确实留下了东西」。
 */
export function CheckpointList({ data }: { data: JobCheckpointsResponse | null }) {
  if (!data || !data.exists) {
    return (
      <Card title="训练产物" className="section-card" data-testid="job-checkpoints">
        <Empty
          description="还没有产物。任务运行后会在这里显示 checkpoint 文件。"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      </Card>
    );
  }

  const weights = data.items.filter((item) => item.is_weights);
  return (
    <Card
      title={`训练产物（${weights.length} 个权重文件，共 ${formatBytes(data.total_bytes)}）`}
      className="section-card"
      data-testid="job-checkpoints"
    >
      <Table
        rowKey="name"
        size="small"
        columns={columns}
        dataSource={data.items}
        pagination={data.items.length > 20 ? { pageSize: 20, size: 'small' } : false}
      />
      {data.truncated && (
        <Typography.Paragraph type="secondary" className="checkpoint-truncated">
          产物过多，仅显示前 200 项。
        </Typography.Paragraph>
      )}
    </Card>
  );
}
