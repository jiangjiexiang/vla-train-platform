import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { DatasetMeta, Recipe } from '../types/api';
import { QuickStartPage, buildOverrides, describeSubmitError, isRecipeCompatible } from './QuickStartPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  const api = {
    getHealth: vi.fn(),
    me: vi.fn(),
    listDatasets: vi.fn(),
    listRecipes: vi.fn(),
    createRun: vi.fn(),
  };
  return { ...actual, api };
});

const { api, ApiError } = await import('../api/client');
const listDatasetsMock = vi.mocked(api.listDatasets);
const listRecipesMock = vi.mocked(api.listRecipes);
const createRunMock = vi.mocked(api.createRun);

function dataset(overrides: Partial<DatasetMeta> = {}): DatasetMeta {
  return {
    id: 'pick1028',
    name: 'pick1028',
    path: '/datasets/pick1028',
    format: 'lerobot-v3.0',
    status: 'ready',
    size_bytes: 3.2 * 1024 ** 3,
    episodes: 1028,
    frames: 512000,
    fps: 30,
    tasks: ['pick the cube'],
    robot_type: 'so100',
    cameras: ['top', 'wrist'],
    image_size: [480, 640],
    state_dim: 6,
    action_dim: 6,
    action_names: [],
    state_names: [],
    duration_seconds: 512000 / 30,
    issues: [],
    scanned_at: '2026-01-01T00:00:00Z',
    scan_depth: 'shallow',
    dir_mtime: 0,
    ...overrides,
  };
}

function recipe(overrides: Partial<Recipe> = {}): Recipe {
  return {
    id: 'smolvla-finetune',
    name: 'SmolVLA 微调',
    description: '单卡 4090 抓取类任务',
    image: 'vla-smolvla:latest',
    entrypoint: 'python /app/train_vla.py',
    dataset_formats: ['lerobot-v3.0', 'lerobot-v2.1'],
    params_template: null,
    defaults: { steps: 2000, batch_size: 16 },
    gpu_count: 1,
    estimated_vram_gb: 12,
    env: {},
    order: 10,
    public: true,
    params: [
      { key: 'steps', flag: '--steps', label: '训练步数', type: 'int', default: 2000, min: 1 },
      { key: 'batch_size', flag: '--batch_size', label: 'batch size', type: 'int', default: 16, min: 1 },
      { key: 'resume', flag: '--resume', label: '断点续训', type: 'bool' },
    ],
    extra_args: null,
    ...overrides,
  };
}

const READY = dataset();
const NOISY = dataset({
  id: 'old2',
  name: 'older-v21',
  format: 'lerobot-v2.1',
  status: 'warning',
  issues: [{ level: 'warning', code: 'fps_mismatch', message: 'meta/fps 与视频帧率不一致' }],
});
const BROKEN = dataset({
  id: 'broken',
  name: 'broken-ds',
  format: 'lerobot-v3.0',
  status: 'unusable',
  issues: [{ level: 'error', code: 'missing_meta', message: '缺少 meta/info.json' }],
});

const SMOLVLA = recipe();
const OPENPI = recipe({
  id: 'openpi-pi05-lora',
  name: 'π0.5 LoRA 微调',
  dataset_formats: ['lerobot-v2.1', 'lerobot-v2.0'],
  estimated_vram_gb: 24,
});
const SMOKE = recipe({
  id: 'smoke-test',
  name: '环境冒烟测试',
  description: '几步小训练，验证 CUDA 与数据挂载',
  estimated_vram_gb: 2,
  order: 1,
  defaults: { steps: 5 },
  params: [{ key: 'steps', flag: '--steps', label: '训练步数', type: 'int', default: 5, min: 1 }],
});

function renderPage(initialEntries: Array<string | { pathname: string; state?: unknown }> = ['/']) {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={initialEntries}>
        <Routes>
          <Route path="/" element={<QuickStartPage />} />
          <Route path="/jobs/:name" element={<div>任务详情页</div>} />
        </Routes>
      </MemoryRouter>
    </AntdApp>,
  );
}

beforeEach(() => {
  listDatasetsMock.mockReset();
  listRecipesMock.mockReset();
  createRunMock.mockReset();
  listDatasetsMock.mockImplementation(() => Promise.resolve({
    items: [READY, NOISY, BROKEN],
    summary: { total: 3, ready: 1, warning: 1, unusable: 1, total_frames: 1024000 },
    root: '/datasets',
  }));
  listRecipesMock.mockImplementation(() => Promise.resolve({ items: [SMOKE, SMOLVLA, OPENPI] }));
  createRunMock.mockImplementation(() => Promise.reject(new Error('unexpected createRun call')));
});

/** 等数据集与配方两次请求都落地：默认配方是列表里的第一个（与后端 order 排序一致）。 */
async function waitForReady() {
  await screen.findByTestId('dataset-card-pick1028');
  await waitFor(() => expect(listRecipesMock).toHaveBeenCalled());
}

describe('QuickStartPage', () => {
  it('不可用的数据集不能选中', async () => {
    renderPage();
    await waitForReady();

    const broken = await screen.findByTestId('dataset-card-broken');
    expect(broken).toHaveAttribute('aria-disabled', 'true');
    expect(within(broken).getByText(/缺少 meta\/info\.json/)).toBeInTheDocument();

    fireEvent.click(broken);
    expect(broken).toHaveAttribute('aria-checked', 'false');

    // 有 warning 的数据集可以选
    const noisy = screen.getByTestId('dataset-card-old2');
    fireEvent.click(noisy);
    expect(noisy).toHaveAttribute('aria-checked', 'true');
    // 选中 warning 数据集后，配方列表按 v2.1 过滤：smoke + smolvla + openpi 都支持
    expect(screen.getByTestId('recipe-card-openpi-pi05-lora')).toBeInTheDocument();
  });

  it('只显示与数据集格式兼容的配方', async () => {
    renderPage();
    await waitForReady();

    fireEvent.click(screen.getByTestId('dataset-card-pick1028'));

    // lerobot-v3.0：openpi 只支持 v2.0/v2.1，必须被过滤掉
    expect(await screen.findByTestId('recipe-card-smolvla-finetune')).toBeInTheDocument();
    expect(screen.getByTestId('recipe-card-smoke-test')).toBeInTheDocument();
    expect(screen.queryByTestId('recipe-card-openpi-pi05-lora')).not.toBeInTheDocument();
    expect(screen.getByText(/2 个配方兼容/)).toBeInTheDocument();
  });

  it('提交时 payload 包含正确的 recipe_id / dataset_id / overrides', async () => {
    createRunMock.mockImplementation(() => Promise.resolve({ name: 'smolvla-pick1028-001' } as never));
    renderPage();
    await waitForReady();

    fireEvent.click(screen.getByTestId('dataset-card-pick1028'));
    await screen.findByTestId('recipe-card-smolvla-finetune');

    // 默认选中第一个兼容配方（smolvla）；改成冒烟测试验证选择会写进 payload
    fireEvent.click(screen.getByTestId('recipe-card-smoke-test'));
    // 自定义一个任务名 + 高优先级
    fireEvent.change(screen.getByLabelText('任务名'), { target: { value: 'my-first-run' } });
    fireEvent.click(screen.getByText('高'));

    // 高级参数展开后，配方默认值已经预填进表单（overrides 只为「改过」的参数生成）
    fireEvent.click(screen.getByText(/高级参数/));
    // 配方的 defaults 已经预填进参数表单（antd 表单初值经 rAF 落地，直接读 DOM）
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    const stepsInput = document.querySelector('[data-testid="param-steps"] input') as HTMLInputElement;
    expect(stepsInput).not.toBeNull();
    console.log('DBG state', JSON.stringify((window as any).__q));
    expect(stepsInput.value).toBe('5');

    fireEvent.click(screen.getByRole('button', { name: /加入训练队列/ }));

    await waitFor(() => expect(createRunMock).toHaveBeenCalledTimes(1));
    const payload = createRunMock.mock.calls[0][0];
    expect(payload.recipe_id).toBe('smoke-test');
    expect(payload.dataset_id).toBe('pick1028');
    expect(payload.priority).toBe('high');
    expect(payload.name).toBe('my-first-run');
    // 没改参数 → 不传 overrides 差异，交给后端用配方 defaults
    expect(payload.overrides).toEqual({});
    expect(payload.env).toBeUndefined();
    // 成功 → 跳到任务详情
    expect(await screen.findByText('任务详情页')).toBeInTheDocument();
  });

  it('422 提交失败时在页面顶部展示后端 detail', async () => {
    createRunMock.mockImplementation(() => Promise.reject(
      new ApiError('配方 SmolVLA 微调 不支持数据集格式 lerobot-v1.0，可用格式：lerobot-v3.0', 422),
    ));
    renderPage();
    await waitForReady();

    fireEvent.click(screen.getByTestId('dataset-card-pick1028'));
    await screen.findByTestId('recipe-card-smolvla-finetune');
    fireEvent.click(screen.getByRole('button', { name: /加入训练队列/ }));

    const alert = await screen.findByTestId('submit-error');
    expect(within(alert).getByText('数据集与配方不兼容')).toBeInTheDocument();
    expect(within(alert).getByText(/不支持数据集格式 lerobot-v1\.0/)).toBeInTheDocument();
    expect(screen.queryByText('任务详情页')).not.toBeInTheDocument();
  });

  it('数据集不可用的 422 提示去修数据集而不是换配方', async () => {
    createRunMock.mockImplementation(() => Promise.reject(
      new ApiError('数据集 broken-ds 不可用：缺少 meta/info.json', 422),
    ));
    renderPage();
    await waitForReady();

    fireEvent.click(screen.getByTestId('dataset-card-pick1028'));
    await screen.findByTestId('recipe-card-smolvla-finetune');
    fireEvent.click(screen.getByRole('button', { name: /加入训练队列/ }));

    const alert = await screen.findByTestId('submit-error');
    expect(within(alert).getByText('数据集不可用')).toBeInTheDocument();
    expect(within(alert).getByText(/修复后重新扫描/)).toBeInTheDocument();
  });

  it('从数据集页带 state 进入时预选该数据集', async () => {
    renderPage([{ pathname: '/', state: { datasetId: 'old2' } }]);
    await waitForReady();

    await waitFor(() => {
      expect(screen.getByTestId('dataset-card-old2')).toHaveAttribute('aria-checked', 'true');
    });
  });

  it('按名称搜索过滤数据集', async () => {
    renderPage();
    await waitForReady();

    fireEvent.change(screen.getByPlaceholderText('搜索数据集名称'), { target: { value: 'broken' } });
    await waitFor(() => {
      expect(screen.queryByTestId('dataset-card-pick1028')).not.toBeInTheDocument();
      expect(screen.getByTestId('dataset-card-broken')).toBeInTheDocument();
    });
  });
});

describe('QuickStartPage 辅助函数', () => {
  it('isRecipeCompatible 按格式精确匹配', () => {
    expect(isRecipeCompatible(SMOLVLA, READY)).toBe(true);
    expect(isRecipeCompatible(OPENPI, READY)).toBe(false);
    expect(isRecipeCompatible(OPENPI, NOISY)).toBe(true);
    expect(isRecipeCompatible(SMOLVLA, null)).toBe(false);
  });

  it('buildOverrides 只保留改动与填写过的参数', () => {
    expect(buildOverrides(SMOLVLA, { steps: 2000, batch_size: 16, resume: false })).toEqual({});
    expect(buildOverrides(SMOLVLA, { steps: 500, batch_size: 16, resume: true })).toEqual({ steps: 500, resume: true });
    expect(buildOverrides(SMOLVLA, { steps: null, unknown: 1 })).toEqual({});
    expect(buildOverrides(null, { steps: 1 })).toEqual({});
  });

  it('describeSubmitError 区分 409 / 422 数据集 / 422 格式', () => {
    expect(describeSubmitError(new ApiError('job foo already exists', 409)).title).toBe('任务名已被占用');
    expect(describeSubmitError(new ApiError('数据集 x 不可用：缺少 meta', 422)).title).toBe('数据集不可用');
    expect(describeSubmitError(new ApiError('配方 A 不支持数据集格式 v9，可用格式：v3.0', 422)).title)
      .toBe('数据集与配方不兼容');
    expect(describeSubmitError(new ApiError('未知的参数：foo', 422)).title).toBe('提交参数有误');
    expect(describeSubmitError(new Error('boom')).description).toBe('boom');
  });
});

// act 警告兜底：antd 的部分内部更新在微任务里落地
afterEach(async () => {
  await act(async () => {
    await Promise.resolve();
  });
});