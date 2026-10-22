/**
 * useApiQuery 的陈旧标记（``stale``）。
 *
 * 列表接口在筛选条件变化时会**继续显示上一页的行**，直到新请求返回。
 * 如果界面不区分这段时间，批量勾选就会作用在不对应当前条件的行上，
 * 甚至把旧选择带进「当前筛选的全部结果」导出。
 *
 * 这里锁定契约：
 * * key 变了 → 在途期间 ``stale === true``（旧数据不可用于批量操作）；
 * * 新数据到达 → ``stale === false``；
 * * 同一 key 的重新取数 → 旧数据仍然对应当前条件，**不是** stale。
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { useApiQuery } from './useApiQuery';
import type { QueryState } from './useApiQuery';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Row = { label: string };

/** 挂起 loader：测试决定每个请求何时返回，从而能观察「在途」窗口。 */
function deferredLoader(responses: Array<(value: Row) => void>) {
  return (signal: AbortSignal): Promise<Row> =>
    new Promise<Row>((resolve, reject) => {
      if (signal.aborted) {
        reject(new DOMException('aborted', 'AbortError'));
        return;
      }
      responses.push(resolve);
    });
}

describe('useApiQuery.stale', () => {
  it('key 变化后在途期间标记为陈旧，响应到达后恢复', async () => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const responses: Array<(value: Row) => void> = [];
    let current!: QueryState<Row>;

    function Probe({ queryKey }: { queryKey: string }): ReactNode {
      current = useApiQuery<Row>(queryKey, deferredLoader(responses));
      return null;
    }

    const render = (queryKey: string) => {
      act(() => {
        root.render(<Probe queryKey={queryKey} />);
      });
    };
    const flush = async () => {
      await act(async () => {
        await Promise.resolve();
      });
    };

    render('a');
    await flush();
    expect(current.loading).toBe(true);
    expect(current.stale).toBe(false);   // 还没有数据

    await act(async () => {
      responses[0]({ label: 'A' });
      await Promise.resolve();
    });
    expect(current.data?.label).toBe('A');
    expect(current.stale).toBe(false);

    // 换条件：旧行仍在屏幕上，但必须被标记为陈旧
    render('b');
    await flush();
    expect(current.data?.label).toBe('A');   // 旧数据仍然显示
    expect(current.loading).toBe(true);
    expect(current.stale).toBe(true);        // 但不能再用于批量操作

    await act(async () => {
      responses[responses.length - 1]({ label: 'B' });
      await Promise.resolve();
    });
    expect(current.data?.label).toBe('B');
    expect(current.stale).toBe(false);

    // 同 key 重新取数：数据仍然对应当前条件，不算陈旧
    act(() => {
      current.reload();
    });
    await flush();
    expect(current.data?.label).toBe('B');
    expect(current.loading).toBe(true);
    expect(current.stale).toBe(false);

    act(() => root.unmount());
    container.remove();
  });

  it('取消后的旧响应不会写回数据', async () => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const responses: Array<(value: Row) => void> = [];
    let current!: QueryState<Row>;

    function Probe({ queryKey }: { queryKey: string }): ReactNode {
      current = useApiQuery<Row>(queryKey, deferredLoader(responses));
      return null;
    }
    const render = (queryKey: string) => {
      act(() => {
        root.render(<Probe queryKey={queryKey} />);
      });
    };
    const flush = async () => {
      await act(async () => {
        await Promise.resolve();
      });
    };

    render('first');
    await flush();
    render('second');
    await flush();

    // 第一个请求的响应这时才回来（已经被取消）
    await act(async () => {
      responses[0]({ label: '过期' });
      await Promise.resolve();
    });
    expect(current.data).toBeNull();

    await act(async () => {
      responses[responses.length - 1]({ label: '最新' });
      await Promise.resolve();
    });
    expect(current.data?.label).toBe('最新');

    act(() => root.unmount());
    container.remove();
  });

  it('enabled=false 时没有数据也不在途', async () => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    let current!: QueryState<Row>;

    function Probe(): ReactNode {
      current = useApiQuery<Row>('k', async () => ({ label: 'x' }), { enabled: false });
      return null;
    }

    await act(async () => {
      root.render(<Probe />);
      await Promise.resolve();
    });
    expect(current.data).toBeNull();
    expect(current.loading).toBe(false);
    expect(current.stale).toBe(false);
    act(() => root.unmount());
    container.remove();
  });
});