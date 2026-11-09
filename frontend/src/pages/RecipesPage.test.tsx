import { fireEvent, render, screen } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Recipe } from '../types/api';
import { RecipesPage } from './RecipesPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return { ...actual, api: { listRecipes: vi.fn() } };
});

const { api } = await import('../api/client');
const listRecipesMock = vi.mocked(api.listRecipes);

const SMOLVLA: Recipe = {
  id: 'smolvla-finetune',
  name: 'SmolVLA 微调',
  description: '单卡 4090 抓取类任务',
  image: 'vla-smolvla:latest',
  entrypoint: 'python /app/train_vla.py',
  dataset_formats: ['lerobot-v3.0'],
  params_template: null,
  defaults: { steps: 2000 },
  gpu_count: 1,
  estimated_vram_gb: 12,
  env: {},
  order: 10,
  public: true,
  params: [
    { key: 'steps', flag: '--steps', label: '训练步数', type: 'int', default: 2000, min: 1, help: '总优化步数' },
    { key: 'device', flag: '--policy.device', label: '训练设备', type: 'enum', default: 'cuda', options: ['cuda', 'cpu'] },
  ],
  extra_args: null,
};

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter>
        <RecipesPage />
      </MemoryRouter>
    </AntdApp>,
  );
}

beforeEach(() => {
  listRecipesMock.mockReset();
  listRecipesMock.mockImplementation(() => Promise.resolve({ items: [SMOLVLA] }));
});

describe('RecipesPage', () => {
  it('列出配方，展开后显示镜像、格式与参数清单', async () => {
    renderPage();

    const title = await screen.findByText('SmolVLA 微调');
    expect(screen.getByText('12 GB 显存')).toBeInTheDocument();

    fireEvent.click(title);

    expect(await screen.findByText('vla-smolvla:latest')).toBeInTheDocument();
    expect(screen.getByText('参数清单（2）')).toBeInTheDocument();
    expect(screen.getByText('训练步数')).toBeInTheDocument();
    expect(screen.getByText('cuda / cpu')).toBeInTheDocument();
  });

  it('空列表时给出提示', async () => {
    listRecipesMock.mockImplementation(() => Promise.resolve({ items: [] }));
    renderPage();

    expect(await screen.findByText('还没有登记任何训练配方')).toBeInTheDocument();
  });
});