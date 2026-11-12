import { useEffect, useState } from 'react';

import { api } from '../../api/client';
import type { Recipe } from '../../types/api';

/** 训练耗时预估。失败就返回 null —— 估不出来不该挡住用户提交。 */
export function useEstimate(recipeId: string | undefined, recipe: Recipe | undefined) {
  const [value, setValue] = useState<{ range: string; hint: string } | null>(null);
  const defaults = (recipe?.defaults ?? {}) as Record<string, unknown>;
  const steps = defaults.steps;

  useEffect(() => {
    if (!recipeId) {
      setValue(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const result = await api.estimateRun({
            recipe_id: recipeId,
            // 预估只用配方参数；后端不强制 dataset，这里给空串即可
            dataset_id: '',
            overrides: steps === undefined ? undefined : { steps: Number(steps) },
          });
          if (cancelled) return;
          if (!result.estimated_seconds) {
            setValue(null);
            return;
          }
          setValue({
            range: result.range_text || result.estimated_text,
            hint:
              result.source === 'history'
                ? `按 ${result.samples} 个同类历史任务实测（约 ${(result as { steps_per_second?: number }).steps_per_second ?? '?'} 步/秒）`
                : '暂无同类历史数据，用的是保守估计',
          });
        } catch {
          if (!cancelled) setValue(null);
        }
      })();
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [recipeId, steps]);

  return value;
}
