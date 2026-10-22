import { useCallback, useEffect, useRef } from 'react';

export interface PollingOptions {
  /** 是否需要轮询（页面不可见且无任务时不必轮询） */
  enabled: boolean;
  /** 前台轮询间隔：2 秒 */
  foregroundMs?: number;
  /** 后台轮询间隔：10 秒 */
  backgroundMs?: number;
}

/**
 * 前台 2 秒 / 后台 10 秒轮询；卸载时清理定时器；同一次回调未结束不重叠调用。
 */
export function usePolling(callback: () => Promise<void> | void, options: PollingOptions): void {
  const { enabled, foregroundMs = 2_000, backgroundMs = 10_000 } = options;
  const latest = useRef(callback);
  latest.current = callback;
  const lastRunAt = useRef(0);
  const inFlight = useRef(false);

  const tick = useCallback(async () => {
    if (inFlight.current) return;
    const now = Date.now();
    const hidden = typeof document !== 'undefined' && document.hidden;
    if (hidden && now - lastRunAt.current < backgroundMs) return;
    inFlight.current = true;
    lastRunAt.current = now;
    try {
      await latest.current();
    } finally {
      inFlight.current = false;
    }
  }, [backgroundMs]);

  useEffect(() => {
    if (!enabled) return;
    void tick();
    const timer = window.setInterval(() => void tick(), foregroundMs);
    const onVisible = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, foregroundMs, tick]);
}