import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../context/AuthContext';
import type { AdminRecipe, CurrentUser } from '../types/api';
import { RecipeAdminPage } from './RecipeAdminPage';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      getHealth: vi.fn(),
      me: vi.fn(),
      logout: vi.fn(),
      adminListRecipes: vi.fn(),
      adminGetRecipe: vi.fn(),
      adminSaveRecipe: vi.fn(),
      adminRestoreRecipe: vi.fn(),
      adminRecipeHistory: vi.fn(),
      adminRollbackRecipe: vi.fn(),
    },
  };
});

const { api, ApiError } = await import('../api/client');
const meMock = vi.mocked(api.me);
const getHealthMock = vi.mocked(api.getHealth);
const listMock = vi.mocked(api.adminListRecipes);
const getMock = vi.mocked(api.adminGetRecipe);
const saveMock = vi.mocked(api.adminSaveRecipe);
const restoreMock = vi.mocked(api.adminRestoreRecipe);
const historyMock = vi.mocked(api.adminRecipeHistory);
const rollbackMock = vi.mocked(api.adminRollbackRecipe);

const ADMIN: CurrentUser = { username: 'root', role: 'admin', source: 'ldap' };

const BUILTIN_RECIPE: AdminRecipe = {
  id: 'smolvla-finetune',
  name: 'SmolVLA 微调',
  description: '',
  image: 'vla-smolvla:latest',
  entrypoint: 'python /app/train_vla.py',
  dataset_formats: ['lerobot-v3.0'],
  params_template: null,
  defaults: {},
  gpu_count: 1,
  estimated_vram_gb: null,
  env: {},
  order: 10,
  public: true,
  params: [],
  extra_args: null,
  source: 'builtin',
  updated_at: null,
  updated_by: null,
};

const OVERRIDDEN_RECIPE: AdminRecipe = {
  ...BUILTIN_RECIPE,
  id: 'rlinf-ppo-maniskill',
  name: 'RLinf PPO（ManiSkill 仿真）',
  source: 'override',
  updated_at: '2026-11-13T08:00:00Z',
  updated_by: 'root@10.0.0.1',
};

function renderPage() {
  getHealthMock.mockImplementation(() =>
    Promise.resolve({ status: 'ok', auth_required: true, auth_mode: 'ldap' }),
  );
  meMock.mockImplementation(() => Promise.resolve(ADMIN));
  localStorage.setItem('vla-platform-token', 'session-token');
  return render(
    <AntdApp>
      <AuthProvider>
        <MemoryRouter>
          <RecipeAdminPage />
        </MemoryRouter>
      </AuthProvider>
    </AntdApp>,
  );
}

beforeEach(() => {
  localStorage.clear();
  for (const mock of [listMock, getMock, saveMock, restoreMock, historyMock, rollbackMock]) {
    mock.mockReset();
  }
  listMock.mockImplementation(() => Promise.resolve({ items: [BUILTIN_RECIPE, OVERRIDDEN_RECIPE] }));
});

describe('RecipeAdminPage', () => {
  it('列出全部配方并区分 默认/已覆盖', async () => {
    renderPage();

    expect(await screen.findByText('SmolVLA 微调')).toBeInTheDocument();
    expect(screen.getByText('RLinf PPO（ManiSkill 仿真）')).toBeInTheDocument();
    expect(screen.getByText('默认')).toBeInTheDocument();
    expect(screen.getByText('已覆盖')).toBeInTheDocument();
    // 覆盖记录展示操作者
    expect(screen.getByText('root@10.0.0.1')).toBeInTheDocument();
  });

  it('编辑 YAML 并保存；校验失败时错误留在弹窗里', async () => {
    getMock.mockImplementation(() =>
      Promise.resolve({ id: 'smolvla-finetune', source: 'builtin', yaml: 'id: smolvla-finetune\n', updated_at: null, updated_by: null }),
    );
    saveMock.mockImplementation(() =>
      Promise.reject(new ApiError('defaults 含模板未声明的参数：nonsense', 422)),
    );
    renderPage();

    const editButtons = await screen.findAllByRole('button', { name: /编辑 YAML/ });
    fireEvent.click(editButtons[0]);

    const textarea = await screen.findByLabelText('配方 YAML');
    expect((textarea as HTMLTextAreaElement).value).toContain('id: smolvla-finetune');

    fireEvent.change(textarea, { target: { value: 'id: smolvla-finetune\n# edited' } });
    fireEvent.click(screen.getByRole('button', { name: '保 存' }));

    await waitFor(() =>
      expect(saveMock).toHaveBeenCalledWith('smolvla-finetune', 'id: smolvla-finetune\n# edited'),
    );
    // 422：弹窗不关闭，错误说明展示在编辑框上方
    expect(await screen.findByText(/defaults 含模板未声明的参数/)).toBeInTheDocument();
    expect(screen.getByLabelText('配方 YAML')).toBeInTheDocument();
  });

  it('恢复默认只在已覆盖时可用，确认后调用 DELETE', async () => {
    restoreMock.mockImplementation(() =>
      Promise.resolve({ id: 'rlinf-ppo-maniskill', source: 'builtin', yaml: '', updated_at: null, updated_by: null, restored: true }),
    );
    renderPage();

    await screen.findByText('SmolVLA 微调');
    const restoreButtons = screen.getAllByRole('button', { name: /恢复默认/ });
    expect(restoreButtons[0]).toBeDisabled(); // builtin
    expect(restoreButtons[1]).not.toBeDisabled(); // override

    fireEvent.click(restoreButtons[1]);
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));

    await waitFor(() => expect(restoreMock).toHaveBeenCalledWith('rlinf-ppo-maniskill'));
  });

  it('历史弹窗展示动作序列，回滚调用 POST rollback', async () => {
    historyMock.mockImplementation(() =>
      Promise.resolve({
        items: [
          { id: 2, action: 'save', changed_at: '2026-11-13T08:00:00Z', changed_by: 'root@10.0.0.1', has_yaml: true },
          { id: 1, action: 'save', changed_at: '2026-11-12T08:00:00Z', changed_by: 'root@10.0.0.1', has_yaml: true },
        ],
      }),
    );
    rollbackMock.mockImplementation(() =>
      Promise.resolve({ id: 'rlinf-ppo-maniskill', source: 'override', yaml: '', updated_at: null, updated_by: null }),
    );
    renderPage();

    const historyButtons = await screen.findAllByRole('button', { name: /历\s*史/ });
    fireEvent.click(historyButtons[1]);

    expect(await screen.findByText('变更历史：rlinf-ppo-maniskill')).toBeInTheDocument();
    expect(screen.getAllByText('保存覆盖')).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: /回滚到上一个版本/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));

    await waitFor(() => expect(rollbackMock).toHaveBeenCalledWith('rlinf-ppo-maniskill'));
  });
});
