import { describe, expect, it } from 'vitest';

import type { ParamsTemplate } from '../types/api';
import {
  BUILTIN_PARAMS_TEMPLATE,
  buildCommandFromTemplate,
  buildParamTokens,
  builtinTemplate,
  isExtraParam,
  resolveBaseCommand,
  resolveExtraArgs,
  shellQuote,
  templateDefaultValues,
} from './buildCommand';

const BASE = 'python /app/train.py';

/** 构造一份最小模板；params 用契约第 1 节的真实样例。 */
function makeTemplate(overrides: Partial<ParamsTemplate> = {}): ParamsTemplate {
  return {
    schema_version: 1,
    entrypoint: 'python /app/train.py --config /config/train_config.yaml',
    params: [
      { key: 'steps', flag: '--steps', label: '训练步数', type: 'int', default: 1000, min: 1, max: 10000000, required: false, help: '总优化步数' },
      { key: 'batch_size', flag: '--batch_size', label: 'batch size', type: 'int', default: 8, min: 1, max: 512 },
      { key: 'lr', flag: '--lr', label: '学习率', type: 'float', default: 1e-4 },
      { key: 'dtype', flag: '--dtype', label: '精度', type: 'enum', default: 'bfloat16', options: ['bfloat16', 'float16', 'float32'] },
      { key: 'freeze_vision', flag: '--freeze_vision_encoder', label: '冻结视觉编码器', type: 'bool', default: false, true_value: '', false_value: null },
      { key: 'resume', flag: '--resume_from', label: '从 checkpoint 恢复', type: 'path', default: null, placeholder: '/checkpoints/<任务名>/checkpoint' },
    ],
    extra_args: { label: '其他参数', help: '原样追加到命令行末尾', enabled: true, default: '' },
    ...overrides,
  };
}

describe('shellQuote（契约 3）', () => {
  it('安全字符原样返回', () => {
    expect(shellQuote('1e-4')).toBe('1e-4');
    expect(shellQuote('/checkpoints/exp001/checkpoint')).toBe('/checkpoints/exp001/checkpoint');
    expect(shellQuote('a_b@c%d+e=f:g,h.i-j/k')).toBe('a_b@c%d+e=f:g,h.i-j/k');
    expect(shellQuote('123')).toBe('123');
  });

  it('含空格的值用单引号包裹', () => {
    expect(shellQuote('float 32')).toBe("'float 32'");
    expect(shellQuote(' a')).toBe("' a'");
    expect(shellQuote('a b c')).toBe("'a b c'");
  });

  it('单引号转义', () => {
    expect(shellQuote("it's")).toBe(`'it'\\''s'`);
    expect(shellQuote("it's a test")).toBe(`'it'\\''s a test'`);
  });

  it('其它 shell 元字符一律进引号', () => {
    expect(shellQuote('a;b')).toBe("'a;b'");
    expect(shellQuote('$HOME')).toBe("'$HOME'");
    expect(shellQuote('a>b')).toBe("'a>b'");
    expect(shellQuote('a\tb')).toBe("'a\tb'");
  });

  it('空字符串被包成空引号', () => {
    expect(shellQuote('')).toBe("''");
  });
});

describe('buildCommandFromTemplate（契约 3）', () => {
  it('空模板 + 空值：只返回 base command', () => {
    const empty: ParamsTemplate = { schema_version: 1, params: [] };
    expect(buildCommandFromTemplate(empty, {}, BASE)).toBe(BASE);
    expect(buildCommandFromTemplate(empty, {})).toBe('');
    expect(buildCommandFromTemplate({ schema_version: 1, params: [], extra_args: { enabled: true } }, {})).toBe('');
    // 只含空格不算「非空内容」，不产生空 token
    expect(buildCommandFromTemplate({ schema_version: 1, params: [], extra_args: { enabled: true } }, { extra_args: '   ' })).toBe('');
  });

  it('int 参数按 params 顺序拼接，值取自用户输入', () => {
    const command = buildCommandFromTemplate(makeTemplate(), { steps: 2000, batch_size: 32 }, BASE);
    expect(command).toBe('python /app/train.py --steps 2000 --batch_size 32 --lr 0.0001 --dtype bfloat16');
  });

  it('未填写的参数回退到 default，default 也没有则跳过', () => {
    // 只有 steps / batch_size / dtype 有默认值；lr 用户填了覆盖默认
    const command = buildCommandFromTemplate(makeTemplate(), { lr: '3e-5' }, BASE);
    expect(command).toBe('python /app/train.py --steps 1000 --batch_size 8 --lr 3e-5 --dtype bfloat16');
  });

  it('float 保持字面形式：1e-4 不会变成 0.0001', () => {
    const command = buildCommandFromTemplate(makeTemplate(), { lr: '1e-4' }, BASE);
    expect(command).toContain('--lr 1e-4');
    expect(command).not.toContain('0.0001');
    // 默认值同为 1e-4，也保持字面形式
    expect(buildCommandFromTemplate(makeTemplate(), {}, BASE)).toContain('--lr 1e-4');
    // 科学计数法大写 E 同样原样保留
    expect(buildCommandFromTemplate(makeTemplate(), { lr: '5E-5' }, BASE)).toContain('--lr 5E-5');
  });

  it('float 默认值来自 JSON number 时与后端 str() 一致（1e-4 → 0.0001）', () => {
    // 模板里的 default: 1e-4 在 JSON 里就是数字 0.0001，前后端都会拼成 0.0001
    const template = makeTemplate({
      params: [{ key: 'lr', flag: '--lr', label: '学习率', type: 'float', default: 1e-4 }],
    });
    expect(buildCommandFromTemplate(template, {}, BASE)).toBe('python /app/train.py --lr 0.0001');
  });

  it('bool：true 且 true_value 不是字符串 → 只拼 flag', () => {
    const template = makeTemplate({
      params: [{ key: 'amp', flag: '--amp', label: '混合精度', type: 'bool', default: false }],
    });
    expect(buildCommandFromTemplate(template, { amp: true }, BASE)).toBe('python /app/train.py --amp');
  });

  it('bool：true 且 true_value 是字符串 → flag + 值；空字符串也只拼 flag', () => {
    const template = makeTemplate({
      params: [
        { key: 'a', flag: '--flag-a', label: 'A', type: 'bool', true_value: 'yes' },
        { key: 'b', flag: '--flag-b', label: 'B', type: 'bool', true_value: '' },
      ],
    });
    expect(buildCommandFromTemplate(template, { a: true, b: true }, BASE)).toBe(
      'python /app/train.py --flag-a yes --flag-b ',
    );
  });

  it('bool：false 且 false_value 不是字符串 → 跳过；是字符串 → flag + 值', () => {
    const skip = makeTemplate({
      params: [{ key: 'amp', flag: '--amp', label: '混合精度', type: 'bool', default: false, false_value: null }],
    });
    expect(buildCommandFromTemplate(skip, { amp: false }, BASE)).toBe(BASE);
    // 缺省（未填写）默认 false，同样跳过
    expect(buildCommandFromTemplate(skip, {}, BASE)).toBe(BASE);

    const withFalse = makeTemplate({
      params: [{ key: 'amp', flag: '--amp', label: '混合精度', type: 'bool', false_value: 'false' }],
    });
    expect(buildCommandFromTemplate(withFalse, { amp: false }, BASE)).toBe('python /app/train.py --amp false');
  });

  it('bool：默认值来自模板 default（true）', () => {
    const template = makeTemplate({
      params: [{ key: 'freeze', flag: '--freeze', label: '冻结', type: 'bool', default: true }],
    });
    expect(buildCommandFromTemplate(template, {}, BASE)).toBe('python /app/train.py --freeze');
  });

  it('enum 取值原样拼接', () => {
    const command = buildCommandFromTemplate(makeTemplate(), { dtype: 'float16' }, BASE);
    expect(command).toContain('--dtype float16');
    expect(command).not.toContain('bfloat16');
  });

  it('str 含空格加单引号，含单引号正确转义', () => {
    const template = makeTemplate({
      params: [{ key: 'note', flag: '--note', label: '备注', type: 'str' }],
    });
    expect(buildCommandFromTemplate(template, { note: 'hello world' }, BASE)).toBe(
      "python /app/train.py --note 'hello world'",
    );
    expect(buildCommandFromTemplate(template, { note: "it's a test" }, BASE)).toBe(
      `python /app/train.py --note 'it'\\''s a test'`,
    );
  });

  it('null / undefined / 空字符串一律跳过', () => {
    const template = makeTemplate({
      params: [
        { key: 'a', flag: '--a', label: 'A', type: 'str' },
        { key: 'b', flag: '--b', label: 'B', type: 'int' },
        { key: 'c', flag: '--c', label: 'C', type: 'path' },
        { key: 'd', flag: '--d', label: 'D', type: 'int' },
      ],
    });
    expect(buildCommandFromTemplate(template, { a: null, b: undefined, c: '', d: 0 }, BASE)).toBe(
      'python /app/train.py --d 0',
    );
  });

  it('flag 允许重复：两个不同 key 用同一个 flag 时各拼一次', () => {
    const template = makeTemplate({
      params: [
        { key: 'resume_a', flag: '--resume', label: '恢复 A', type: 'path' },
        { key: 'resume_b', flag: '--resume', label: '恢复 B', type: 'path' },
      ],
    });
    expect(buildCommandFromTemplate(template, { resume_a: '/a', resume_b: '/b' }, BASE)).toBe(
      'python /app/train.py --resume /a --resume /b',
    );
  });

  it('path 含空格时加引号（checkpoint 目录可能带空格）', () => {
    expect(buildCommandFromTemplate(makeTemplate(), { resume: '/ckpt/my run/step-10' }, BASE)).toBe(
      'python /app/train.py --steps 1000 --batch_size 8 --lr 1e-4 --dtype bfloat16 --resume_from \'/ckpt/my run/step-10\'',
    );
  });

  it('extra_args 原样追加到最后，不加引号、不解析', () => {
    const command = buildCommandFromTemplate(makeTemplate(), { extra_args: '--foo 1 --bar="a b"' }, BASE);
    expect(command).toBe(
      'python /app/train.py --steps 1000 --batch_size 8 --lr 1e-4 --dtype bfloat16 --foo 1 --bar="a b"',
    );
  });

  it('extra_args.enabled=false 时不追加', () => {
    const template = makeTemplate({ extra_args: { enabled: false } });
    expect(buildCommandFromTemplate(template, { extra_args: '--foo 1' }, BASE)).toBe(
      'python /app/train.py --steps 1000 --batch_size 8 --lr 1e-4 --dtype bfloat16',
    );
  });

  it('extra_args 为空或只有空白时跳过', () => {
    const template = makeTemplate({ params: [] });
    expect(buildCommandFromTemplate(template, { extra_args: '' }, BASE)).toBe(BASE);
    expect(buildCommandFromTemplate(template, { extra_args: '   ' }, BASE)).toBe(BASE);
    expect(buildCommandFromTemplate(template, { extra_args: null }, BASE)).toBe(BASE);
  });

  it('base command 优先级：用户手填 > entrypoint > default_command', () => {
    const template = makeTemplate({ params: [] });
    expect(resolveBaseCommand({ userCommand: 'python mine.py', entrypoint: 'python ep.py', defaultCommand: 'python cfg.py' }))
      .toBe('python mine.py');
    expect(resolveBaseCommand({ userCommand: '   ', entrypoint: 'python ep.py', defaultCommand: 'python cfg.py' }))
      .toBe('python ep.py');
    expect(resolveBaseCommand({ userCommand: '', entrypoint: null, defaultCommand: 'python cfg.py' }))
      .toBe('python cfg.py');
    expect(resolveBaseCommand({ userCommand: '', entrypoint: '', defaultCommand: '' })).toBe('');
  });

  it('entrypoint 本身不进 buildCommandFromTemplate 的拼接，只用 baseCommand', () => {
    const template = makeTemplate({ params: [{ key: 'steps', flag: '--steps', label: '步数', type: 'int' }] });
    expect(buildCommandFromTemplate(template, { steps: 10 }, template.entrypoint ?? undefined)).toBe(
      'python /app/train.py --config /config/train_config.yaml --steps 10',
    );
    // 用户手填的命令覆盖 entrypoint
    expect(buildCommandFromTemplate(template, { steps: 10 }, 'python /other.py')).toBe('python /other.py --steps 10');
  });
});

describe('内置通用模板（契约 4）', () => {
  it('与契约第 4 节完全一致', () => {
    expect(BUILTIN_PARAMS_TEMPLATE).toEqual({
      schema_version: 1,
      entrypoint: null,
      params: [
        { key: 'train_steps', flag: '--steps', label: '训练步数', type: 'int', placeholder: '如 1000' },
        { key: 'batch_size', flag: '--batch-size', label: 'batch size', type: 'int', placeholder: '如 8' },
        { key: 'grad_accum', flag: '--grad-accum', label: '梯度累积', type: 'int', placeholder: '如 4' },
        { key: 'learning_rate', flag: '--lr', label: '学习率', type: 'str', placeholder: '如 1e-4' },
        { key: 'save_every', flag: '--save-freq', label: '保存 checkpoint 间隔（步）', type: 'int', placeholder: '如 500' },
        { key: 'log_every', flag: '--log-freq', label: '打印日志间隔（步）', type: 'int', placeholder: '如 10' },
        { key: 'num_workers', flag: '--num-workers', label: '数据加载进程数', type: 'int', placeholder: '如 4' },
        { key: 'dtype', flag: '--dtype', label: '精度', type: 'enum', options: ['bfloat16', 'float16', 'float32'] },
        { key: 'extra_args', flag: null, label: '其他参数', type: 'str', kind: 'extra' },
      ],
      extra_args: { label: '其他参数', enabled: true },
    });
  });

  it('builtinTemplate() 返回可安全修改的副本', () => {
    const copy = builtinTemplate();
    copy.params[0].label = '被改了';
    expect(BUILTIN_PARAMS_TEMPLATE.params[0].label).toBe('训练步数');
    expect(builtinTemplate().params).not.toBe(BUILTIN_PARAMS_TEMPLATE.params);
  });

  it('兼容写法 kind:"extra" 的参数不重复拼接', () => {
    const extraParam = BUILTIN_PARAMS_TEMPLATE.params[8];
    expect(isExtraParam(extraParam)).toBe(true);
    expect(
      buildCommandFromTemplate(
        BUILTIN_PARAMS_TEMPLATE,
        { train_steps: 1000, batch_size: 8, extra_args: '--freeze-vision-encoder' },
        BASE,
      ),
    ).toBe('python /app/train.py --steps 1000 --batch-size 8 --freeze-vision-encoder');
  });
});

describe('辅助函数', () => {
  it('buildParamTokens 跳过 flag 为空的参数', () => {
    expect(buildParamTokens({ params: [{ key: 'x', flag: null, label: 'X', type: 'str' }] }, { x: 'v' })).toEqual([]);
  });

  it('resolveExtraArgs 使用 spec.default 兜底', () => {
    expect(resolveExtraArgs({ enabled: true, default: '--default-arg' }, {})).toBe('--default-arg');
    expect(resolveExtraArgs({ enabled: true, default: '--default-arg' }, { extra_args: '--user-arg' })).toBe('--user-arg');
    expect(resolveExtraArgs(undefined, { extra_args: '--x' })).toBe('');
  });

  it('templateDefaultValues 展开默认值，bool 显式给 false', () => {
    expect(templateDefaultValues(makeTemplate())).toEqual({
      steps: 1000,
      batch_size: 8,
      lr: 1e-4,
      dtype: 'bfloat16',
      freeze_vision: false,
      resume: null,
    });
    expect(templateDefaultValues(null)).toEqual({});
  });
});