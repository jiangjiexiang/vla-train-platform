import { describe, expect, it } from 'vitest';

import { cloneName, formatDateTime, formatDuration, percent } from './format';

describe('formatDateTime', () => {
  it('returns an em dash for empty values', () => {
    expect(formatDateTime(null)).toBe('—');
    expect(formatDateTime(undefined)).toBe('—');
    expect(formatDateTime('')).toBe('—');
  });

  it('formats an ISO timestamp with the zh-CN locale', () => {
    const iso = '2024-06-01T12:34:56Z';
    expect(formatDateTime(iso)).toBe(
      new Date(iso).toLocaleString('zh-CN', { hour12: false }),
    );
  });

  it('returns the original value when the date cannot be parsed', () => {
    expect(formatDateTime('not-a-date')).toBe('not-a-date');
  });
});

describe('formatDuration', () => {
  it('returns an em dash for null and undefined', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(undefined)).toBe('—');
  });

  it('formats seconds, minutes, hours and days', () => {
    expect(formatDuration(0)).toBe('0秒');
    expect(formatDuration(59)).toBe('59秒');
    expect(formatDuration(60)).toBe('1分 0秒');
    expect(formatDuration(61)).toBe('1分 1秒');
    expect(formatDuration(3661)).toBe('1时 1分 1秒');
    expect(formatDuration(90061)).toBe('1天 1时 1分');
  });

  it('floors fractional seconds and clamps negatives to zero', () => {
    expect(formatDuration(90.9)).toBe('1分 30秒');
    expect(formatDuration(-30)).toBe('0秒');
  });
});

describe('percent', () => {
  it('computes a rounded, clamped percentage', () => {
    expect(percent(0, 0)).toBe(0);
    expect(percent(5, 0)).toBe(0);
    expect(percent(1, 3)).toBe(33);
    expect(percent(5, 4)).toBe(100);
  });
});

describe('cloneName', () => {
  it('appends a copy suffix and trims trailing dashes', () => {
    expect(cloneName('job-1')).toBe('job-1-copy');
    expect(cloneName(`${'a'.repeat(44)}--`)).toBe(`${'a'.repeat(44)}-copy`);
  });
});
