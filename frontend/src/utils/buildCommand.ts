import type { ExtraArgsSpec, ParamSpec, ParamsTemplate } from '../types/api';

/**
 * 训练参数模板 → 命令行的拼装逻辑。
 *
 * 这是 .agents/PARAMS-TEMPLATE-CONTRACT.md 第 3 节的**前端实现**，与后端保持逐字节一致：
 * 后端最终会把 command 交给 `/bin/bash -lc` 执行，前端预览必须能原样预测它。
 * 任何行为改动都要同时改后端，并同步 utils/buildCommand.test.ts。
 */

/** 契约 3：只含这些字符的值不加引号，原样输出。 */
const SAFE_VALUE_PATTERN = /^[A-Za-z0-9_@%+=:,./-]+$/;

/**
 * 契约 3 里 `String(v)` 的实现，与后端 Python 的 `str(v)` 对齐：
 * - number：JS 与 Python 都用最短往返表示，整数不会带 `.0`（`-0` 单独处理成 "-0"）。
 * - 注意：JSON 里的 `1e-4` 在两端都会解析成 0.0001，拼出来就是 "0.0001"。
 *   想保持 `1e-4` 字面形式，必须让值以**字符串**进入（表单 float 用 InputNumber stringMode）。
 * bool 走 buildBoolToken，不会进这里。
 */
function pythonStr(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') {
    if (Object.is(value, -0)) return '-0';
    if (Number.isSafeInteger(value)) return String(value);
    return String(value);
  }
  return String(value);
}

/** 契约 3 的「空值」：null / undefined / 空字符串。 */
export function isEmptyValue(value: unknown): boolean {
  return value === null || value === undefined || value === '';
}

/**
 * 契约 3 的 shell_quote：
 * - 匹配 `^[A-Za-z0-9_@%+=:,./-]+$` → 原样返回
 * - 否则用单引号包裹，内部的每个 `'` 替换为 `'\''`
 *
 * 注意：其它空白字符（制表符、连续空格）也算「不安全」，整体进入单引号；
 * 空字符串因为不匹配上面的字符集，会被包成 `''`。
 */
export function shellQuote(value: string): string {
  if (SAFE_VALUE_PATTERN.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** 值是否「已填写」：null / undefined / 空字符串都视为未填（契约 3）。 */
function isBlank(value: unknown): boolean {
  return isEmptyValue(value);
}

/** bool 参数当成「真」处理：真值 / 数字 1 / 字符串 "true"、"1"（后端 JSON 里只可能是前两种）。 */
function isTruthyParam(value: unknown): boolean {
  if (value === true) return true;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const text = value.trim().toLowerCase();
    return text === 'true' || text === '1';
  }
  return false;
}

/**
 * 单个 bool 参数拼出的 token；null 表示该参数不出现（false 且 false_value 不是字符串）。
 *   true  → true_value 是字符串时 `flag true_value`，否则只有 flag
 *   false → false_value 是字符串时 `flag false_value`，否则跳过
 */
function buildBoolToken(param: ParamSpec, value: unknown): string | null {
  const flag = param.flag ?? '';
  if (isTruthyParam(value)) {
    return typeof param.true_value === 'string' ? `${flag} ${param.true_value}` : flag;
  }
  return typeof param.false_value === 'string' ? `${flag} ${param.false_value}` : null;
}

/** 单个非 bool 参数拼出的 token；null 表示跳过。 */
function buildValueToken(param: ParamSpec, value: unknown): string | null {
  if (isBlank(value)) return null;
  return `${param.flag ?? ''} ${shellQuote(pythonStr(value))}`;
}

/**
 * 参数取值：用户填写的值优先，未填则用模板默认值；都没有时返回 undefined（跳过）。
 * 这里用 hasOwnProperty 而不是 `??`：用户显式清空 InputNumber 得到 null，
 * 应视为「未填」从而回退到默认值，而不是把 null 当成一次覆盖。
 */
function resolveValue(param: ParamSpec, values: Record<string, unknown>): unknown {
  if (Object.prototype.hasOwnProperty.call(values, param.key)) {
    const provided = values[param.key];
    if (!isBlank(provided)) return provided;
  }
  return param.default;
}

/** 按契约 3 处理 params 数组，返回拼好的 token 列表。 */
export function buildParamTokens(
  template: Pick<ParamsTemplate, 'params'>,
  values: Record<string, unknown> = {},
): string[] {
  const tokens: string[] = [];
  for (const param of template.params ?? []) {
    // 内置模板的兼容写法：flag 为 null 的 str 参数（kind: "extra"）不是独立 flag，
    // 它的值走 extra_args 的「原样追加」通道，这里不重复处理。
    if (!param.flag) continue;
    const value = resolveValue(param, values);
    if (value === undefined) continue;
    const token = param.type === 'bool' ? buildBoolToken(param, value) : buildValueToken(param, value);
    if (token !== null) tokens.push(token);
  }
  return tokens;
}

/** 顶层 extra_args 的值：用户填写的优先，否则用 spec.default；enabled 为 false 时不追加。 */
export function resolveExtraArgs(spec: ExtraArgsSpec | undefined, values: Record<string, unknown>): string {
  if (!spec || spec.enabled === false) return '';
  const provided = values.extra_args;
  const value = !isBlank(provided) ? provided : spec.default;
  if (isBlank(value) || String(value).trim() === '') return '';
  // 原样追加：不引号包裹、不解析。
  return String(value);
}

/**
 * 由模板拼装最终命令行（契约 3）：
 *   base command + 按 params 顺序的 token + extra_args
 *
 * - 基础命令优先级由调用方决定：用户手填 > 模板 entrypoint > /config 的 default_command，
 *   这里只接收已经选好的 baseCommand。
 * - `extra_args.enabled` 且用户填了非空内容时，原样追加到末尾（不加引号、不解析）。
 */
export function buildCommandFromTemplate(
  template: ParamsTemplate,
  values: Record<string, unknown> = {},
  baseCommand?: string,
): string {
  const parts: string[] = [];
  const base = (baseCommand ?? '').trim();
  if (base) parts.push(base);
  parts.push(...buildParamTokens(template, values));
  const extra = resolveExtraArgs(template.extra_args, values);
  if (extra) parts.push(extra);
  return parts.join(' ');
}

/**
 * 选出最终使用的基础命令（契约 6）：
 *   用户手填的命令 > 模板 entrypoint > /config 的 default_command
 * 三者都为空时返回空串（此时命令预览显示占位文案）。
 */
export function resolveBaseCommand(options: {
  userCommand?: string | null;
  entrypoint?: string | null;
  defaultCommand?: string | null;
}): string {
  const userCommand = (options.userCommand ?? '').trim();
  if (userCommand) return userCommand;
  const entrypoint = (options.entrypoint ?? '').trim();
  if (entrypoint) return entrypoint;
  return (options.defaultCommand ?? '').trim();
}

/** 把模板默认值展开成表单初值：bool 一律显式给 false，enum 缺省时给第一项以便下拉有值。 */
export function templateDefaultValues(template: ParamsTemplate | null): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  if (!template) return result;
  for (const param of template.params ?? []) {
    if (!param.flag) continue;
    if (param.type === 'bool') {
      result[param.key] = isBlank(param.default) ? false : isTruthyParam(param.default);
      continue;
    }
    result[param.key] = isBlank(param.default) ? null : param.default;
  }
  return result;
}

/**
 * 契约 4 的内置通用模板：镜像没有登记模板（或拉取失败）时前端自己的兜底。
 * 与后端返回的 `source: "builtin"` 模板**完全一致**（含 extra_args 参数的 kind: "extra" 兼容写法）。
 */
export const BUILTIN_PARAMS_TEMPLATE: ParamsTemplate = {
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
};

/** 返回一份内置模板的副本，避免调用方误改常量。 */
export function builtinTemplate(): ParamsTemplate {
  return {
    ...BUILTIN_PARAMS_TEMPLATE,
    params: BUILTIN_PARAMS_TEMPLATE.params.map((param) => ({ ...param })),
    extra_args: { ...BUILTIN_PARAMS_TEMPLATE.extra_args },
  };
}

/** 判断一个参数是不是内置模板的「透传参数」（flag 为空 + kind: "extra"）。 */
export function isExtraParam(param: ParamSpec): boolean {
  return !param.flag && param.kind === 'extra';
}