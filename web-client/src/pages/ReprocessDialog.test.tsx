/**
 * 生成识别候选对话框（Task C）。
 *
 * 弹窗保持既有 props {unit, onClose, onQueued}，但语义从“强制重新识别”改为“生成候选”：
 * 只排队候选任务，不改写现用文字；读取两种配置并把省资源配置作为默认；
 * 文件页范围由全部 unit 摘要解析成显式 unit_ids（去重、文件页不是印刷页、上限 1000）。
 * 全部断言走真实 React DOM + 受控 fetch。
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { reauthenticate, resetSession } from '../api/client';
import type { JobItem, UnitDetail, UnitSummary } from '../api/types';
import { ReprocessDialog } from './ReprocessDialog';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface PendingCall {
  url: string;
  method: string;
  body: unknown;
  responded: boolean;
  respond: (status: number, payload: unknown) => void;
}

let pending: PendingCall[] = [];
let unitDataset: UnitSummary[] | null = null;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** 受控 fetch：/workspace 与文献单元分页自动应答；其余挂起，由测试决定何时返回。 */
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
      if (unitDataset && method === 'GET' && /^\/api\/v1\/documents\/[^/]+\/units$/.test(parsed.pathname)) {
        const page = Number(parsed.searchParams.get('page') ?? '1');
        const pageSize = Number(parsed.searchParams.get('page_size') ?? '30');
        const start = (page - 1) * pageSize;
        return jsonResponse(200, {
          items: unitDataset.slice(start, start + pageSize),
          total: unitDataset.length,
          page,
          page_size: pageSize,
        });
      }
      const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
      return await new Promise<Response>((resolve) => {
        pending.push({
          url,
          method,
          body,
          responded: false,
          respond: (status, payload) => resolve(jsonResponse(status, payload)),
        });
      });
    }),
  );
}

function findCall(fragment: string, method?: string): number {
  return pending.findIndex(
    (call) => !call.responded && call.url.includes(fragment) && (method ? call.method === method : true),
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 60; i += 1) await Promise.resolve();
  });
}

async function respondTo(index: number, status: number, payload: unknown): Promise<void> {
  const call = pending[index];
  if (!call) throw new Error(`没有第 ${index} 个待响应请求`);
  call.responded = true;
  await act(async () => {
    call.respond(status, payload);
    await Promise.resolve();
  });
  await settle();
}

async function click(node: HTMLElement): Promise<void> {
  await act(async () => {
    node.click();
  });
  await settle();
}

async function setInputValue(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
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

function radioByValue(container: HTMLElement, value: string): HTMLInputElement {
  const node = container.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`);
  if (!node) throw new Error(`未找到单选项：${value}`);
  return node;
}

interface DialogProps {
  unit: UnitDetail;
  onClose: () => void;
  onQueued: (message: string) => void;
}

async function mountDialog(props: DialogProps): Promise<{ container: HTMLElement; unmount: () => void }> {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<ReprocessDialog {...props} />);
  });
  await settle();
  return {
    container,
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}

function unitFixture(overrides: Partial<UnitDetail> = {}): UnitDetail {
  return {
    unit_id: 'unit-a',
    doc_id: 'doc-1',
    title: '某县志',
    filename: 'xian-zhi.pdf',
    kind: 'page',
    seq: 12,
    file_page: 12,
    printed_page: null,
    printed_page_status: null,
    title_path: null,
    method: 'ocr',
    status: 'completed',
    notes: null,
    text: '媒妁之言，父母之命。',
    original_text: '媒妁之言，父母之命。',
    corrected_text: null,
    version: 'original',
    text_sha256: 'hash-current',
    content_revision: 7,
    ocr: null,
    revisions: [],
    ...overrides,
  };
}

function unitSummary(id: string, filePage: number | null): UnitSummary {
  return {
    id,
    doc_id: 'doc-1',
    seq: filePage ?? 1,
    kind: filePage === null ? 'docx' : 'page',
    file_page: filePage,
    printed_page: null,
    status: 'completed',
    method: 'ocr',
    title_path: null,
  };
}

function profileItem(id: 'compatible-v1' | 'small-lowmem-v1', available = true, reason: string | null = null) {
  return {
    id,
    label: id === 'small-lowmem-v1' ? '省资源配置' : '兼容配置',
    description: id === 'small-lowmem-v1' ? '逐页处理、减小单次识字批次。' : '保留当前默认识别行为。',
    available,
    reason,
  };
}

const PROFILES = { items: [profileItem('compatible-v1'), profileItem('small-lowmem-v1')] };

function jobItem(overrides: Partial<JobItem> = {}): JobItem {
  return {
    id: 'job-1',
    filename: 'xian-zhi.pdf',
    doc_id: 'doc-1',
    kind: 'ocr_candidate',
    state: 'queued',
    stage: null,
    total_pages: 1,
    processed_pages: 0,
    completed_pages: 0,
    failed_pages: 0,
    pending_pages: 1,
    searchable_pages: 0,
    duplicate: false,
    error: null,
    pause_requested: false,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

beforeEach(async () => {
  pending = [];
  unitDataset = null;
  installFetch();
  resetSession();
  await reauthenticate();
  pending = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
});

describe('ReprocessDialog（生成识别候选）', () => {
  it('读取两种配置，默认选择可用的省资源配置', async () => {
    const harness = await mountDialog({ unit: unitFixture(), onClose: vi.fn(), onQueued: vi.fn() });
    const profilesIndex = findCall('/ocr/profiles');
    expect(profilesIndex).toBeGreaterThanOrEqual(0);
    await respondTo(profilesIndex, 200, PROFILES);

    expect(radioByValue(harness.container, 'small-lowmem-v1').checked).toBe(true);
    expect(radioByValue(harness.container, 'compatible-v1').checked).toBe(false);
    expect(harness.container.textContent).toContain('省资源配置');
    harness.unmount();
  });

  it('不可用的配置被禁用并给出原因，默认回退到可用项', async () => {
    const harness = await mountDialog({ unit: unitFixture(), onClose: vi.fn(), onQueued: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, {
      items: [profileItem('compatible-v1'), profileItem('small-lowmem-v1', false, '本机缺少省资源模型文件。')],
    });

    expect(radioByValue(harness.container, 'small-lowmem-v1').disabled).toBe(true);
    expect(harness.container.textContent).toContain('本机缺少省资源模型文件。');
    expect(radioByValue(harness.container, 'compatible-v1').checked).toBe(true);
    harness.unmount();
  });

  it('当前页提交候选任务：只排队，不改写单元文字', async () => {
    const onClose = vi.fn();
    const onQueued = vi.fn();
    const harness = await mountDialog({
      unit: unitFixture({ unit_id: 'unit-a', doc_id: 'doc-1' }),
      onClose,
      onQueued,
    });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    expect(harness.container.textContent).not.toContain('强制重新识别');
    await click(buttonByText(harness.container, '生成候选'));

    const createIndex = findCall('/documents/doc-1/ocr-candidate-jobs', 'POST');
    expect(createIndex).toBeGreaterThanOrEqual(0);
    expect(pending[createIndex].body).toEqual({
      unit_ids: ['unit-a'],
      profile_id: 'small-lowmem-v1',
      layout: 'original',
    });
    await respondTo(createIndex, 202, jobItem({ id: 'job-9', kind: 'ocr_candidate' }));

    expect(onQueued).toHaveBeenCalledTimes(1);
    const message = String(onQueued.mock.calls[0][0]);
    expect(message).toContain('候选');
    expect(message).toContain('待核对');
    expect(onClose).toHaveBeenCalledTimes(1);

    // 只排队：没有直接改写单元，也没有旧 reprocess 调用
    expect(pending.some((call) => call.method === 'PATCH')).toBe(false);
    expect(pending.some((call) => call.url.includes('/reprocess'))).toBe(false);
    expect(pending.some((call) => call.url.includes('/documents/doc-1/units'))).toBe(false);
    harness.unmount();
  });

  it('提交失败时保留输入与选择', async () => {
    const harness = await mountDialog({ unit: unitFixture(), onClose: vi.fn(), onQueued: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);
    await click(buttonByText(harness.container, '生成候选'));

    await respondTo(findCall('/ocr-candidate-jobs', 'POST'), 500, {
      error: { code: 'SERVER_ERROR', message: '服务暂时不可用，请稍后重试。' },
    });

    expect(harness.container.textContent).toContain('服务暂时不可用');
    expect(radioByValue(harness.container, 'small-lowmem-v1').checked).toBe(true);
    expect(radioByValue(harness.container, 'page').checked).toBe(true);
    expect(buttonByText(harness.container, '生成候选').disabled).toBe(false);
    harness.unmount();
  });

  it('文件页范围：按 file_page 解析去重并展示真实页数', async () => {
    unitDataset = [
      unitSummary('u10', 10),
      unitSummary('u11', 11),
      unitSummary('u12', 12),
      unitSummary('u13', 13),
      unitSummary('u14', 14),
    ];
    const harness = await mountDialog({
      unit: unitFixture({ unit_id: 'u12', doc_id: 'doc-1', file_page: 12 }),
      onClose: vi.fn(),
      onQueued: vi.fn(),
    });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    await click(radioByValue(harness.container, 'range'));
    await settle();
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-from')!, '11');
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-to')!, '12');

    expect(harness.container.textContent).toMatch(/将生成\s*2\s*页/);

    await click(buttonByText(harness.container, '生成候选'));
    const createIndex = findCall('/documents/doc-1/ocr-candidate-jobs', 'POST');
    expect(createIndex).toBeGreaterThanOrEqual(0);
    expect(pending[createIndex].body).toEqual({
      unit_ids: ['u11', 'u12'],
      profile_id: 'small-lowmem-v1',
      layout: 'original',
    });
    harness.unmount();
  });

  it('超出已知文件页时给出前端解释且不提交', async () => {
    unitDataset = [unitSummary('u10', 10), unitSummary('u11', 11)];
    const harness = await mountDialog({ unit: unitFixture({ unit_id: 'u10', doc_id: 'doc-1' }), onClose: vi.fn(), onQueued: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    await click(radioByValue(harness.container, 'range'));
    await settle();
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-from')!, '90');
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-to')!, '95');

    expect(harness.container.textContent).toContain('超出');
    await click(buttonByText(harness.container, '生成候选'));
    expect(findCall('/ocr-candidate-jobs', 'POST')).toBe(-1);
    harness.unmount();
  });

  it('Word 文献没有文件页时拒绝并解释', async () => {
    unitDataset = [unitSummary('w1', null), unitSummary('w2', null)];
    const harness = await mountDialog({ unit: unitFixture({ unit_id: 'w1', doc_id: 'doc-1' }), onClose: vi.fn(), onQueued: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    await click(radioByValue(harness.container, 'document'));
    await settle();

    expect(harness.container.textContent).toContain('Word');
    await click(buttonByText(harness.container, '生成候选'));
    expect(findCall('/ocr-candidate-jobs', 'POST')).toBe(-1);
    harness.unmount();
  });

  it('单页模式遇到 Word 单元也拒绝（候选只支持 PDF 文件页）', async () => {
    const harness = await mountDialog({
      unit: unitFixture({ unit_id: 'w1', doc_id: 'doc-1', kind: 'docx', file_page: null }),
      onClose: vi.fn(),
      onQueued: vi.fn(),
    });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    expect(radioByValue(harness.container, 'page').checked).toBe(true);
    expect(harness.container.textContent).toContain('Word');
    await click(buttonByText(harness.container, '生成候选'));
    expect(findCall('/ocr-candidate-jobs', 'POST')).toBe(-1);
    harness.unmount();
  });

  it('文件页终点超出已知范围时解释且不提交（不静默截断）', async () => {
    unitDataset = [unitSummary('u10', 10), unitSummary('u11', 11)];
    const harness = await mountDialog({
      unit: unitFixture({ unit_id: 'u10', doc_id: 'doc-1', file_page: 10 }),
      onClose: vi.fn(),
      onQueued: vi.fn(),
    });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    await click(radioByValue(harness.container, 'range'));
    await settle();
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-from')!, '10');
    await setInputValue(harness.container.querySelector<HTMLInputElement>('#ocr-range-to')!, '95');

    expect(harness.container.textContent).toContain('超出');
    await click(buttonByText(harness.container, '生成候选'));
    expect(findCall('/ocr-candidate-jobs', 'POST')).toBe(-1);
    harness.unmount();
  });

  it('文件页列表读取失败时显示真实原因并可重试', async () => {
    const harness = await mountDialog({ unit: unitFixture(), onQueued: vi.fn(), onClose: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);
    await click(radioByValue(harness.container, 'range'));
    await respondTo(findCall('/documents/doc-1/units'), 500, {
      error: { code: 'INTERNAL', message: '文件页列表暂不可读' },
    });
    expect(harness.container.textContent).toContain('文件页列表暂不可读');
    expect(buttonByText(harness.container, '生成候选').disabled).toBe(true);
    await click(buttonByText(harness.container, '重试页列表'));
    expect(findCall('/documents/doc-1/units')).toBeGreaterThanOrEqual(0);
    harness.unmount();
  });

  it('关闭弹窗后入队响应不再触发旧页面导航', async () => {
    const onQueued = vi.fn();
    const onClose = vi.fn();
    const harness = await mountDialog({ unit: unitFixture(), onQueued, onClose });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);
    await click(buttonByText(harness.container, '生成候选'));
    const request = findCall('/ocr-candidate-jobs', 'POST');
    expect(request).toBeGreaterThanOrEqual(0);
    harness.unmount();
    await respondTo(request, 202, jobItem());
    expect(onQueued).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('整本文献超过 1000 页时拒绝', async () => {
    unitDataset = Array.from({ length: 1001 }, (_, index) => unitSummary(`u${index + 1}`, index + 1));
    const harness = await mountDialog({ unit: unitFixture({ unit_id: 'u1', doc_id: 'doc-1' }), onClose: vi.fn(), onQueued: vi.fn() });
    await respondTo(findCall('/ocr/profiles'), 200, PROFILES);

    await click(radioByValue(harness.container, 'document'));
    await settle();

    expect(harness.container.textContent).toContain('1000');
    await click(buttonByText(harness.container, '生成候选'));
    expect(findCall('/ocr-candidate-jobs', 'POST')).toBe(-1);
    harness.unmount();
  });
});
