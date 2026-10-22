/**
 * 任务页对候选任务（kind=ocr_candidate）的计数措辞（Task C R1 回归）。
 *
 * completed_pages 对候选任务只是“推理完成页数”，不能被当成“已采用”；待核对页数单独统计。
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { resetSession } from '../api/client';
import type { JobItem } from '../api/types';
import { AppProvider } from '../state/AppState';
import { JobsPage } from './JobsPage';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const CANDIDATE_JOB: JobItem = {
  id: 'job-1',
  filename: 'xian-zhi.pdf',
  doc_id: 'doc-1',
  kind: 'ocr_candidate',
  state: 'running',
  stage: null,
  total_pages: 10,
  processed_pages: 4,
  completed_pages: 4,
  failed_pages: 0,
  pending_pages: 0,
  searchable_pages: 0,
  duplicate: false,
  error: null,
  pause_requested: false,
  review_pending_pages: 3,
  created_at: null,
  updated_at: null,
};
let currentJob = CANDIDATE_JOB;

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = String(input);
      if (url.endsWith('/workspace')) {
        return jsonResponse(200, {
          name: '个人图书馆',
          root: 'C:/data/library',
          csrf_token: 'token-1',
          ocr: { available: true },
          coverage: {},
        });
      }
      const parsed = new URL(url, 'http://local');
      if (parsed.pathname.endsWith('/jobs')) {
        return jsonResponse(200, { items: [currentJob], total: 1, page: 1, page_size: 30 });
      }
      if (parsed.pathname.endsWith('/jobs/job-1/units')) return jsonResponse(200, {
        items: [{ unit_id: 'page-70', position: 0, file_page: 70, state: 'done', attempts: 1, error: '' }],
        total: 1, page: 1, page_size: 30,
      });
      if (parsed.pathname.endsWith('/documents/doc-1')) return jsonResponse(200, { filetype: 'pdf' });
      return jsonResponse(200, {});
    }),
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 60; i += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  currentJob = CANDIDATE_JOB;
  installFetch();
  resetSession();
});

it('普通导入带零待核计数时仍显示已完成', async () => {
  currentJob = { ...CANDIDATE_JOB, kind: 'import', state: 'completed', review_pending_pages: 0 };
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(<AppProvider><JobsPage /></AppProvider>); });
    await settle();
    expect(container.textContent).toMatch(/已完成\s*4/);
    expect(container.textContent).not.toContain('推理完成');
    expect(container.textContent).not.toContain('不代表已人工采用');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it('候选任务逐页列表显示真实文件页而非任务内序号', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(<AppProvider><JobsPage /></AppProvider>); });
    await settle();
    const expand = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('查看逐页状态'))!;
    await act(async () => expand.click());
    await settle();
    expect(container.textContent).toContain('文件第 70 页');
    expect(container.textContent).not.toContain('文件第 1 页');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
});

it('候选任务把 completed_pages 标为推理完成，并单独显示待核对页数', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <AppProvider>
          <JobsPage />
        </AppProvider>,
      );
    });
    await settle();

    expect(container.textContent).toMatch(/推理完成\s*4/);
    expect(container.textContent).toMatch(/待核对\s*3/);
    // 不能把推理完成页数说成「已完成」。
    expect(container.textContent).not.toMatch(/已完成\s*4/);
    expect(container.textContent).toContain('不代表已人工采用');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
