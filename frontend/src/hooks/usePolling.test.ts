import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePolling } from './usePolling';

async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('usePolling', () => {
  it('runs immediately and then once per interval', async () => {
    const callback = vi.fn();

    renderHook(() => usePolling(callback, 1000));

    expect(callback).toHaveBeenCalledTimes(1);
    await flushMicrotasks();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(callback).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(callback).toHaveBeenCalledTimes(4);
  });

  it('stops polling and aborts the active signal after unmount', async () => {
    const signals: AbortSignal[] = [];
    const callback = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
    });

    const { unmount } = renderHook(() => usePolling(callback, 500));
    await flushMicrotasks();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(callback).toHaveBeenCalledTimes(2);

    unmount();
    expect(signals).toHaveLength(2);
    // Only the most recent run is still active; the hook aborts that controller.
    expect(signals[signals.length - 1].aborted).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(callback).toHaveBeenCalledTimes(2);
  });

  it('restarts immediately with the latest callback when resetKey changes', async () => {
    const signals: AbortSignal[] = [];
    const callback = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
    });

    const { rerender } = renderHook(
      ({ resetKey }: { resetKey: string }) => usePolling(callback, 1000, true, resetKey),
      { initialProps: { resetKey: 'page-1' } },
    );
    expect(callback).toHaveBeenCalledTimes(1);

    rerender({ resetKey: 'page-2' });
    expect(callback).toHaveBeenCalledTimes(2);
    await flushMicrotasks();

    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(callback).toHaveBeenCalledTimes(3);
  });

  it('does nothing while disabled and starts when enabled becomes true', async () => {
    const callback = vi.fn();

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => usePolling(callback, 1000, enabled),
      { initialProps: { enabled: false } },
    );
    await flushMicrotasks();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(callback).not.toHaveBeenCalled();

    rerender({ enabled: true });
    expect(callback).toHaveBeenCalledTimes(1);

    await flushMicrotasks();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(callback).toHaveBeenCalledTimes(2);
  });
});
