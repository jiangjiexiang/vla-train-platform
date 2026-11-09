import { useEffect, useRef, useState } from 'react';

export function usePolling(
  callback: (signal: AbortSignal) => void | Promise<void>,
  interval = 4000,
  enabled = true,
) {
  const callbackRef = useRef(callback);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return;

    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    const run = async () => {
      controller = new AbortController();
      try {
        await callbackRef.current(controller.signal);
      } finally {
        if (active) timer = setTimeout(run, interval);
      }
    };

    void run();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [enabled, interval, revision]);

  return () => setRevision((value) => value + 1);
}
