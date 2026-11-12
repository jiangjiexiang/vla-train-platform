import type { FormInstance } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { api, getErrorMessage, isAbortError } from '../../api/client';
import { PARAMS_FIELD } from '../../components/TrainParamsForm';
import type { ImageParamsResponse, ParamsTemplate } from '../../types/api';
import { builtinTemplate, templateDefaultValues } from '../../utils/buildCommand';
import type { SubmitFormValues } from './utils';

export function useImageParams(image: string | undefined, form: FormInstance<SubmitFormValues>) {
  const [template, setTemplate] = useState<ParamsTemplate | null>(null);
  const [source, setSource] = useState<ImageParamsResponse['source'] | null>(null);
  const [warning, setWarning] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);

  useEffect(() => {
    const normalizedImage = (image ?? '').trim();
    const currentRequestId = requestId.current + 1;
    requestId.current = currentRequestId;

    const applyTemplate = (
      next: ParamsTemplate,
      nextSource: ImageParamsResponse['source'],
      nextWarning?: string,
    ) => {
      if (requestId.current !== currentRequestId) return;
      setTemplate(next);
      setSource(nextSource);
      setWarning(nextWarning);
      form.setFieldValue(PARAMS_FIELD, templateDefaultValues(next));
    };

    if (!normalizedImage) {
      applyTemplate(builtinTemplate(), 'builtin');
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    api
      .getImageParams(normalizedImage, controller.signal)
      .then((response) => {
        if (response?.template && Array.isArray(response.template.params)) {
          applyTemplate(response.template, response.source, response.warning);
        } else {
          applyTemplate(builtinTemplate(), 'error', '镜像参数模板格式无效，已回退到平台通用参数');
        }
      })
      .catch((reason) => {
        if (isAbortError(reason)) return;
        applyTemplate(builtinTemplate(), 'error', `读取镜像参数模板失败（${getErrorMessage(reason)}），已回退到平台通用参数`);
      })
      .finally(() => {
        if (requestId.current === currentRequestId) setLoading(false);
      });

    return () => controller.abort();
  }, [image, form]);

  return { template, source, warning, loading };
}
