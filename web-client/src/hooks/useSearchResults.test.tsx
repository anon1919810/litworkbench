import { act } from 'react';
import { createRoot } from 'react-dom/client';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { reauthenticate, resetSession } from '../api/client';
import type { SearchResponse } from '../api/types';
import { useSearchResults } from './useSearchResults';
import type { SearchOptions } from './useSearchResults';

// React 19 的 act 环境开关
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const EMPTY_COVERAGE = {
  known_pdf_pages: null,
  covered_pdf_pages: null,
  pending_pdf_pages: null,
  failed_pdf_pages: null,
  unknown_documents: null,
  indexed_docx: 0,
  total_docx: 0,
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function searchPayload(query: string, revision: number, total = 1): SearchResponse {
  return {
    items: [
      {
        unit_id: `u-${query}`,
        doc_id: 'doc-1',
        title: '某县志',
        filename: 'xian-zhi.pdf',
        kind: 'page',
        file_page: 7,
        printed_page: '775',
        title_path: '第三十一篇 文物',
        method: 'ocr',
        status: 'completed',
        snippet: `${query} 的原文片段`,
        start_offset: 0,
        end_offset: 2,
        text_sha256: 'hash-1',
        version: 'original',
      },
    ],
    total,
    page: 1,
    page_size: 30,
    coverage: EMPTY_COVERAGE,
    index_revision: revision,
    total_documents: 1,
  };
}

interface PendingSearch {
  body: Record<string, unknown>;
  respond: (status: number, payload: unknown) => void;
}

const pending: PendingSearch[] = [];

/** 受控的 fetch：/workspace 直接成功（不进 pending），检索请求挂起，由测试决定何时返回。 */
function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (String(url).endsWith('/workspace')) {
        return jsonResponse(200, {
          name: '个人图书馆',
          root: 'C:/data',
          csrf_token: 'token',
          ocr: { available: true },
          coverage: EMPTY_COVERAGE,
        });
      }
      const body = JSON.parse(String(init.body ?? '{}')) as Record<string, unknown>;
      return await new Promise<Response>((resolve) => {
        pending.push({ body, respond: (status, payload) => resolve(jsonResponse(status, payload)) });
      });
    }),
  );
}

function renderHook<P, T>(useHook: (props: P) => T, initialProps: P) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  let current!: T;
  let props = initialProps;

  function Probe(): ReactNode {
    current = useHook(props);
    return null;
  }

  const render = () => {
    act(() => {
      root.render(<Probe />);
    });
  };

  render();
  return {
    get value() {
      return current;
    },
    setProps(next: P) {
      props = next;
      render();
    },
    async flush() {
      await act(async () => {
        await Promise.resolve();
      });
    },
    async respond(index: number, status: number, payload: unknown) {
      await act(async () => {
        pending[index].respond(status, payload);
        await Promise.resolve();
      });
    },
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}

beforeEach(async () => {
  pending.length = 0;
  installFetch();
  resetSession();
  await reauthenticate(); // 建立会话；握手请求不进入 pending
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
});

const OPTIONS: SearchOptions = { query: '文庙', mode: 'phrase', docIds: [], enabled: true };

describe('检索状态机', () => {
  it('首次检索按第一页发出，响应到达后给出结果', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    expect(pending).toHaveLength(1);
    expect(pending[0].body).toMatchObject({ query: '文庙', mode: 'phrase', page: 1, page_size: 30 });
    expect(pending[0].body.expected_index_revision).toBeUndefined();

    await hook.respond(0, 200, searchPayload('文庙', 7));
    expect(hook.value.response?.total).toBe(1);
    expect(hook.value.page).toBe(1);
    hook.unmount();
  });

  it('翻到第 3 页后改变检索方式：下一次请求回到第一页且不带旧代次', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    await hook.respond(0, 200, searchPayload('文庙', 7));

    hook.value.setPage(3);
    await hook.flush();
    expect(pending[1].body).toMatchObject({ page: 3, expected_index_revision: 7 });
    await hook.respond(1, 200, searchPayload('文庙', 7, 90));
    expect(hook.value.page).toBe(3);

    hook.setProps({ ...OPTIONS, mode: 'all_terms' });
    await hook.flush();
    expect(hook.value.page).toBe(1);
    expect(pending[2].body).toMatchObject({ mode: 'all_terms', page: 1 });
    expect(pending[2].body.expected_index_revision).toBeUndefined();
    hook.unmount();
  });

  it('改变来源范围同样回到第一页', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    await hook.respond(0, 200, searchPayload('文庙', 7));
    hook.value.setPage(2);
    await hook.flush();
    await hook.respond(1, 200, searchPayload('文庙', 7, 60));

    hook.setProps({ ...OPTIONS, docIds: ['doc-2'] });
    await hook.flush();
    expect(hook.value.page).toBe(1);
    expect(pending[2].body).toMatchObject({ page: 1, doc_ids: ['doc-2'] });
    hook.unmount();
  });

  it('清空查询后结果被清掉，迟到的响应不能把旧结果带回来', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    await hook.respond(0, 200, searchPayload('文庙', 7));
    expect(hook.value.response).not.toBeNull();

    // 发起一次慢请求
    hook.value.setPage(2);
    await hook.flush();
    expect(pending).toHaveLength(2);

    // 用户清空查询
    hook.setProps({ ...OPTIONS, query: '', enabled: false });
    await hook.flush();
    expect(hook.value.response).toBeNull();

    // 慢请求这时才返回：必须被丢弃
    await hook.respond(1, 200, searchPayload('文庙', 7, 90));
    expect(hook.value.response).toBeNull();
    expect(hook.value.loading).toBe(false);
    hook.unmount();
  });

  it('旧查询慢返回不会覆盖新查询的结果', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    expect(pending).toHaveLength(1);

    // 新查询先发，旧查询还挂着
    hook.setProps({ ...OPTIONS, query: '学宫' });
    await hook.flush();
    expect(pending).toHaveLength(2);

    await hook.respond(1, 200, searchPayload('学宫', 9));
    expect(hook.value.response?.items[0].unit_id).toBe('u-学宫');

    await hook.respond(0, 200, searchPayload('文庙', 7, 90));
    expect(hook.value.response?.items[0].unit_id).toBe('u-学宫');
    expect(hook.value.response?.total).toBe(1);
    hook.unmount();
  });

  it('翻页遇到 409 SEARCH_INDEX_CHANGED：保留条件、回第一页重查且不带旧代次', async () => {
    const hook = renderHook(useSearchResults, OPTIONS);
    await hook.flush();
    await hook.respond(0, 200, searchPayload('文庙', 7));
    hook.value.setPage(2);
    await hook.flush();
    expect(pending[1].body).toMatchObject({ page: 2, expected_index_revision: 7 });

    await hook.respond(1, 409, {
      error: { code: 'SEARCH_INDEX_CHANGED', message: '资料已更新，请用相同条件重新查询第一页。' },
    });

    expect(hook.value.notice).toContain('索引代次');
    expect(hook.value.page).toBe(1);
    await hook.flush();
    expect(pending[2].body).toMatchObject({ query: '文庙', mode: 'phrase', page: 1 });
    expect(pending[2].body.expected_index_revision).toBeUndefined();
    hook.unmount();
  });
});