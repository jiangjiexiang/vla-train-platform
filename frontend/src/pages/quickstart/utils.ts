import { ApiError, getErrorMessage } from '../../api/client';
import { visibleParams } from '../../components/TrainParamsForm';
import type {
  DatasetMeta,
  ParamsTemplate,
  PreflightCheck,
  Priority,
  Recipe,
} from '../../types/api';

export const PRIORITY_OPTIONS: Array<{ value: Priority; label: string }> = [
  { value: 'low', label: '低' },
  { value: 'normal', label: '普通' },
  { value: 'high', label: '高' },
];

/** 配方能否吃下这个数据集：格式必须精确匹配（与后端 check_compatibility 一致）。 */
export function isRecipeCompatible(recipe: Recipe, dataset: DatasetMeta | null): boolean {
  if (!dataset) return false;
  return (recipe.dataset_formats ?? []).includes(dataset.format);
}

/** 把配方列表项转成 TrainParamsForm 需要的模板结构。 */
export function recipeTemplate(recipe: Recipe): ParamsTemplate {
  return {
    schema_version: 1,
    entrypoint: recipe.entrypoint,
    params: recipe.params ?? [],
    extra_args: recipe.extra_args ?? undefined,
  };
}

/**
 * 参数初始值：模板 default 为主，配方的 defaults 覆盖模板（与后端
 * runs.merge_values 的优先级 defaults > default 一致）。bool 缺省显式给 false，
 * 保证 Switch 受控；enum 缺省时取第一个选项。
 */
export function recipeInitialParams(recipe: Recipe): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const param of visibleParams(recipeTemplate(recipe))) {
    if (param.type === 'bool') {
      values[param.key] = false;
    } else if (param.default !== undefined && param.default !== null && param.default !== '') {
      values[param.key] = param.default;
    } else if (param.type === 'enum' && param.options && param.options.length > 0) {
      values[param.key] = param.options[0];
    } else {
      values[param.key] = null;
    }
  }
  for (const [key, value] of Object.entries(recipe.defaults ?? {})) {
    if (key in values) values[key] = value;
  }
  return values;
}

/** 只把用户真正改过的参数放进 overrides，未改的交给后端用 defaults/模板默认值。 */
export function buildOverrides(
  recipe: Recipe | null,
  params: Record<string, unknown> | undefined,
): Record<string, unknown> {
  if (!recipe || !params) return {};
  const initial = recipeInitialParams(recipe);
  const overrides: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (key === 'extra_args') continue;
    if (value === null || value === undefined || value === '') continue;
    if (!(key in initial)) continue;
    if (value !== initial[key]) overrides[key] = value;
  }
  return overrides;
}

/**
 * 422 的 detail 分两种：数据集本身不可用，或配方不支持该格式。两者处理方式完全不同，
 * 提示要分开写，否则用户会去换配方而问题其实在数据上。
 */
export function describeSubmitError(error: unknown): { title: string; description: string } {
  const text = getErrorMessage(error);
  if (error instanceof ApiError && error.status === 409) {
    return {
      title: '任务名已被占用',
      description: `${text}。请换一个任务名，或清空任务名让平台自动生成。`,
    };
  }
  if (error instanceof ApiError && error.status === 422) {
    if (/数据集.*不可用|dataset.*unavailable/i.test(text)) {
      return {
        title: '数据集不可用',
        description: `${text}。请到「数据集」页查看问题并修复后重新扫描，或换一个可用数据集。`,
      };
    }
    if (/不支持数据集格式|不支持|格式/i.test(text)) {
      return {
        title: '数据集与配方不兼容',
        description: `${text}。请换一个支持该格式的配方，或先把数据集转换格式。`,
      };
    }
    return { title: '提交参数有误', description: text };
  }
  return { title: '提交失败', description: text };
}

/** 预检 error → 页面顶部 Alert 的标题与说明。 */
export function describePreflightErrors(errors: PreflightCheck[]): { title: string; description: string } {
  const codes = new Set(errors.map((check) => check.code));
  const title = codes.has('dataset_unusable') || codes.has('no_camera')
    ? '数据集不可用，先修好再训练'
    : codes.has('format_mismatch')
      ? '数据集与配方不兼容'
      : '预检未通过';
  const description = errors.map((check) => check.message).join('；') || '请按下方建议处理后重试';
  return { title, description };
}
