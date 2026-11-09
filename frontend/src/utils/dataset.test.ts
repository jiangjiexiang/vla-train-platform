import { describe, expect, it } from 'vitest';

import { formatBytes, formatCount, formatMinutes, formatParamDefault, paramPlaceholder } from './dataset';

describe('dataset 展示格式化', () => {
  it('formatBytes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(916)).toBe('916 B');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(4.5 * 1024 ** 3)).toBe('4.5 GB');
    expect(formatBytes(null)).toBe('0 B');
  });

  it('formatMinutes', () => {
    expect(formatMinutes(0)).toBe('—');
    expect(formatMinutes(45)).toBe('45 秒');
    expect(formatMinutes(90)).toBe('1.5 分钟');
    expect(formatMinutes(600)).toBe('10 分钟');
    expect(formatMinutes(3900)).toBe('1 小时 5 分钟');
  });

  it('formatCount', () => {
    expect(formatCount(1024000)).toBe('1,024,000');
    expect(formatCount(undefined)).toBe('0');
  });

  it('paramPlaceholder 优先用显式 placeholder，否则回退默认值', () => {
    expect(paramPlaceholder({ default: 5 })).toBe('5');
    expect(paramPlaceholder({ default: 5, placeholder: '例如 100' })).toBe('例如 100');
    expect(paramPlaceholder({ default: null })).toBeUndefined();
  });

  it('formatParamDefault', () => {
    expect(formatParamDefault(null)).toBe('—');
    expect(formatParamDefault(false)).toBe('关闭');
    expect(formatParamDefault('cuda')).toBe('cuda');
    expect(formatParamDefault({ a: 1 })).toBe('{"a":1}');
  });
});