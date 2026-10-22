import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError } from '../lib/conflict';
import type { ErrorInfo } from '../lib/conflict';
import { usePolling } from './usePolling';

export interface QueryState<T> {
  data: T | null;
  loading: boolean;
  error: ErrorInfo | null;
  /** 手动重新取数（写操作后调用） */
  reload: () => void;
  /** 当前生效的查询键。界面用它判断「手上的数据是不是这一次条件的结果」。 */
  key: string;
  /**
   * ``data`` 来自**上一个** key（新条件仍在加载或加载失败）。
   *
   * 这段时间里列表仍然显示旧行，但这些行不再对应当前条件：
   * 勾选框与批量操作必须禁用，不能拿它们去批量改或导出。
   */
  stale: boolean;
}

/**
 * 轻量只读查询：key 变化或 reload 时重新取数，旧请求被取消，过期响应被丢弃。
 * 只把结果放在 React state，不做任何缓存快照；需要轮询的页面传 poll，缺少主键时传 enabled:false。
 */
export function useApiQuery<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  options: { poll?: boolean; enabled?: boolean } = {},
): QueryState<T> {
  const enabled = options.enabled ?? true;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [nonce, setNonce] = useState(0);
  /** data 是由哪个 key 取回来的（同 key 的重新取数不会让它变旧） */
  const [settledKey, setSettledKey] = useState<string | null>(null);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const keyRef = useRef(key);
  keyRef.current = key;
  const controllerRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, []);

  const run = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const seq = (seqRef.current += 1);
    setLoading(true);
    try {
      const result = await loaderRef.current(controller.signal);
      if (seq !== seqRef.current || !mountedRef.current) return;
      setData(result);
      setSettledKey(keyRef.current);
      setError(null);
    } catch (caught) {
      if (seq !== seqRef.current || !mountedRef.current) return;
      const info = describeError(caught);
      if (info.kind === 'aborted') return;
      setError(info);
    } finally {
      if (seq === seqRef.current && mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      controllerRef.current?.abort();
      setData(null);
      setError(null);
      setLoading(false);
      setSettledKey(null);
      return;
    }
    void run();
    return () => controllerRef.current?.abort();
  }, [enabled, key, nonce, run]);

  usePolling(() => run(), { enabled: Boolean(options.poll) });

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  return { data, loading, error, reload, key, stale: data !== null && settledKey !== key };
}
