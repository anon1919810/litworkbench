/**
 * 阅读器全局「保存摘录」在候选/旧 OCR 显示时必须禁用（Task C 回归）。
 *
 * 面板独立渲染候选文字、不共用正式摘录元素；但阅读器的全局按钮也必须随对照侧切换禁用，
 * 否则用户仍可能把候选切片当成正式摘录保存。这里挂载真实 Reader + AppProvider，
 * 只替身 HTTP 响应。
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { resetSession } from '../api/client';
import type { DocumentItem, Page, UnitDetail, UnitSummary } from '../api/types';
import { AppProvider } from '../state/AppState';
import { Reader } from './Reader';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const UNIT: UnitDetail = {
  unit_id: 'u1',
  doc_id: 'doc-1',
  title: '某县志',
  filename: 'xian-zhi.pdf',
  kind: 'page',
  seq: 1,
  file_page: 1,
  printed_page: null,
  printed_page_status: null,
  title_path: null,
  method: 'ocr',
  status: 'completed',
  notes: null,
  text: '现用文字甲',
  original_text: '旧底稿乙',
  corrected_text: null,
  version: 'original',
  text_sha256: 'hash-1',
  content_revision: 7,
  ocr: null,
  revisions: [],
};

const DOCUMENT: DocumentItem = {
  id: 'doc-1',
  title: '某县志',
  filename: 'xian-zhi.pdf',
  filetype: 'pdf',
  page_count: 20,
  unit_count: 1,
  status: 'completed',
  pending_count: 0,
  searchable_pages: 1,
  author: null,
  year: null,
  edition: null,
  volume: null,
};

const UNIT_SUMMARY: UnitSummary = {
  id: 'u1',
  doc_id: 'doc-1',
  seq: 1,
  kind: 'page',
  file_page: 1,
  printed_page: null,
  status: 'completed',
  method: 'ocr',
  title_path: null,
};

function emptyPage<T>(): Page<T> {
  return { items: [], total: 0, page: 1, page_size: 30 };
}

const UNIT2: UnitDetail = { ...UNIT, unit_id: 'u2', seq: 2, file_page: 2, text: '现用文字二' };

interface HeldCall {
  url: string;
  responded: boolean;
  respond: (status: number, body: unknown) => void;
}

let heldUnitIds = new Set<string>();
let heldCalls: HeldCall[] = [];

function respondHeld(index: number, status: number, body: unknown): Promise<void> {
  const call = heldCalls[index];
  if (!call) throw new Error(`没有第 ${index} 个挂起的单元详情请求`);
  call.responded = true;
  call.respond(status, body);
  return settle();
}

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
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
      const method = init.method ?? 'GET';
      if (method === 'GET' && parsed.pathname.endsWith('/units/u1/ocr-candidates')) {
        return jsonResponse(200, emptyPage());
      }
      if (method === 'GET' && parsed.pathname.endsWith('/units/u2/ocr-candidates')) {
        return jsonResponse(200, emptyPage());
      }
      if (method === 'GET' && parsed.pathname.endsWith('/documents/doc-1/units')) {
        return jsonResponse(200, { items: [UNIT_SUMMARY], total: 1, page: 1, page_size: 30 });
      }
      if (method === 'GET' && parsed.pathname.endsWith('/documents/doc-1')) {
        return jsonResponse(200, DOCUMENT);
      }
      const unitMatch = /\/units\/([^/]+)$/.exec(parsed.pathname);
      if (method === 'GET' && unitMatch) {
        const unitId = decodeURIComponent(unitMatch[1]);
        if (heldUnitIds.has(unitId)) {
          return await new Promise<Response>((resolve) => {
            heldCalls.push({ url, responded: false, respond: (status, body) => resolve(jsonResponse(status, body)) });
          });
        }
        if (unitId === 'u1') return jsonResponse(200, UNIT);
        if (unitId === 'u2') return jsonResponse(200, UNIT2);
      }
      return jsonResponse(200, {});
    }),
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 60; i += 1) await Promise.resolve();
  });
}

async function click(node: HTMLElement): Promise<void> {
  await act(async () => {
    node.click();
  });
  await settle();
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll('button')).find((node) =>
    (node.textContent ?? '').includes(text),
  );
  if (!button) throw new Error(`未找到按钮：${text}`);
  return button as HTMLButtonElement;
}

beforeEach(() => {
  heldUnitIds = new Set();
  heldCalls = [];
  installFetch();
  resetSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
});

it('候选/旧 OCR 显示时禁用全局「保存摘录」，切回现用文字后恢复', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <AppProvider>
          <Reader unitId="u1" onSelectUnit={() => {}} />
        </AppProvider>,
      );
    });
    await settle();

    // 未打开对照：全局按钮应可点击。
    expect(buttonByText(container, '保存摘录').disabled).toBe(false);

    await click(buttonByText(container, '对照'));
    // 面板默认显示候选侧：禁用，避免把候选切片保存成正式摘录。
    expect(buttonByText(container, '保存摘录').disabled).toBe(true);
    expect(container.querySelector('.cmp-text .ocr-candidate-panel')).not.toBeNull();
    expect(container.querySelector('.cmp .unit-text')).toBeNull();

    await click(buttonByText(container, '现用文字'));
    expect(buttonByText(container, '保存摘录').disabled).toBe(false);
    expect(container.querySelector('.ocr-candidate-panel .unit-text')?.textContent).toBe(UNIT.text);

    await click(buttonByText(container, '旧 OCR 底稿'));
    expect(buttonByText(container, '保存摘录').disabled).toBe(true);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it('切换 unitId 时先显示加载，不暴露上一页详情与操作', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <AppProvider>
          <Reader unitId="u1" onSelectUnit={() => {}} />
        </AppProvider>,
      );
    });
    await settle();
    // 默认是「原页」标签，先切到识别文字才能看到正文。
    await click(buttonByText(container, '识别文字'));
    expect(container.textContent).toContain('现用文字甲');

    // 新单元详情挂起：旧详情过期，不能继续当作新页展示。
    heldUnitIds = new Set(['u2']);
    await act(async () => {
      root.render(
        <AppProvider>
          <Reader unitId="u2" onSelectUnit={() => {}} />
        </AppProvider>,
      );
    });
    await settle();

    expect(container.textContent).not.toContain('现用文字甲');
    expect(container.textContent).toContain('正在读取单元');
    expect(container.querySelector('button')).toBeNull();

    await respondHeld(0, 200, UNIT2);
    expect(container.textContent).toContain('现用文字二');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
