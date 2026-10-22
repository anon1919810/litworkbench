/**
 * OCR 候选对照面板（Task C）。
 *
 * 全部断言走真实 React DOM + 受控 fetch（只替身 HTTP 端点响应，不替身组件）。
 * 覆盖：按页取数、翻页晚结果不串页、就绪/修订号门禁、采用/保留、409 保留对照、
 * 候选文字不复用正式摘录元素、差异开关、运行中轮询在切页/卸载后清理。
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { reauthenticate, resetSession } from '../api/client';
import type { JobItem, JobUnitItem, OcrCandidateDetail, Page, UnitDetail } from '../api/types';
import { OcrCandidatesPanel } from './OcrCandidatesPanel';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface PendingCall {
  url: string;
  method: string;
  body: unknown;
  responded: boolean;
  respond: (status: number, payload: unknown) => void;
}

let pending: PendingCall[] = [];
/** 候选列表请求总数（含已响应），用于断言轮询是否真的停止。 */
let listRequests = 0;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** 受控 fetch：/workspace 直接成功；其余请求挂起，由测试决定何时返回。 */
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
      const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
      if (url.includes('/units/') && url.includes('/ocr-candidates')) listRequests += 1;
      return await new Promise<Response>((resolve) => {
        pending.push({
          url,
          method: init.method ?? 'GET',
          body,
          responded: false,
          respond: (status, payload) => resolve(jsonResponse(status, payload)),
        });
      });
    }),
  );
}

function candidateListIndex(unitId: string): number {
  return pending.findIndex((call) => !call.responded && call.url.includes(`/units/${unitId}/ocr-candidates`));
}

function findCall(fragment: string, method?: string): number {
  return pending.findIndex(
    (call) => !call.responded && call.url.includes(fragment) && (method ? call.method === method : true),
  );
}

/** 精确匹配路径尾巴，避免 /jobs/{id} 误配 /jobs/{id}/units。 */
function pathTailIndex(pathname: string): number {
  return pending.findIndex((call) => !call.responded && new URL(call.url, 'http://local').pathname.endsWith(pathname));
}

function jobCallIndex(jobId: string): number {
  return pathTailIndex(`/jobs/${jobId}`);
}

function jobUnitsCallIndex(jobId: string): number {
  return pathTailIndex(`/jobs/${jobId}/units`);
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 40; i += 1) await Promise.resolve();
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

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll('button')).find((node) =>
    (node.textContent ?? '').includes(text),
  );
  if (!button) throw new Error(`未找到按钮：${text}`);
  return button as HTMLButtonElement;
}

interface PanelProps {
  unit: UnitDetail;
  onSaved: () => void;
  onSelectUnit?: (unitId: string) => void;
  jobId?: string;
}

interface PanelHarness {
  container: HTMLElement;
  rerender: (props: PanelProps) => Promise<void>;
  unmount: () => void;
}

async function mountPanel(props: PanelProps): Promise<PanelHarness> {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<OcrCandidatesPanel {...props} />);
  });
  await settle();
  return {
    container,
    async rerender(next) {
      await act(async () => {
        root.render(<OcrCandidatesPanel {...next} />);
      });
      await settle();
    },
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

function candidateResult(text: string): OcrCandidateDetail['result'] {
  return {
    text,
    lines: [],
    engine: 'rapidocr',
    model_fingerprints: [],
    config: {},
    timing: {},
    memory: {},
    coordinate_system: 'render_pixels',
    render_width: 1000,
    render_height: 1400,
  };
}

function candidateFixture(overrides: Partial<OcrCandidateDetail> = {}): OcrCandidateDetail {
  return {
    id: 'cand-1',
    job_id: 'job-1',
    attempt_id: 'attempt-1',
    unit_id: 'unit-a',
    doc_id: 'doc-1',
    file_page: 12,
    state: 'ready',
    base_content_revision: 7,
    profile_id: 'small-lowmem-v1',
    layout: 'original',
    result: candidateResult('媒约之言，父母之命。'),
    error: null,
    created_at: '2026-10-16T00:00:00Z',
    decided_at: null,
    ...overrides,
  };
}

function pageOf(items: OcrCandidateDetail[]): Page<OcrCandidateDetail> {
  return { items, total: items.length, page: 1, page_size: 30 };
}

function jobFixture(overrides: Partial<JobItem> = {}): JobItem {
  return {
    id: 'job-9',
    filename: 'xian-zhi.pdf',
    doc_id: 'doc-1',
    kind: 'ocr_candidate',
    state: 'running',
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

function jobUnit(unitId: string, candidateState: JobUnitItem['candidate_state']): JobUnitItem {
  return {
    unit_id: unitId,
    position: 0,
    state: 'done',
    attempts: 1,
    error: null,
    updated_at: null,
    candidate_id: candidateState ? `cand-${unitId}` : null,
    candidate_state: candidateState,
  };
}

beforeEach(async () => {
  pending = [];
  listRequests = 0;
  installFetch();
  resetSession();
  await reauthenticate();
  pending = [];
  listRequests = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
  vi.useRealTimers();
});

describe('OcrCandidatesPanel', () => {
  it('按当前页取候选；候选文字独立渲染，不提供正式摘录入口', async () => {
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture(), onSaved });

    const listIndex = candidateListIndex('unit-a');
    expect(listIndex).toBeGreaterThanOrEqual(0);
    expect(pending[listIndex].url).toContain('page=1');
    expect(pending[listIndex].url).toContain('page_size=30');
    await respondTo(listIndex, 200, pageOf([candidateFixture()]));

    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('媒约之言');

    // 候选文字必须是独立元素，不能复用阅读器保存摘录的 .unit-text
    expect(harness.container.querySelector('.unit-text')).toBeNull();
    expect(
      Array.from(harness.container.querySelectorAll('button')).some((button) =>
        (button.textContent ?? '').includes('保存摘录'),
      ),
    ).toBe(false);

    // 只读当前页候选：不取整本书、不取原图、不多取单条详情
    expect(
      pending.some(
        (call) =>
          call.url.includes('/render') ||
          call.url.includes('/source') ||
          call.url.includes('/documents/doc-1/units') ||
          call.url.includes('/ocr-candidates/cand-1'),
      ),
    ).toBe(false);
    expect(onSaved).not.toHaveBeenCalled();
    harness.unmount();
  });

  it('传入 jobId 时按任务过滤候选列表', async () => {
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-1' });
    const listIndex = candidateListIndex('unit-a');
    expect(listIndex).toBeGreaterThanOrEqual(0);
    expect(pending[listIndex].url).toContain('job_id=job-1');
    await respondTo(listIndex, 200, pageOf([]));
    harness.unmount();
  });

  it('切换单元后，旧单元的迟到候选响应不串页', async () => {
    const harness = await mountPanel({ unit: unitFixture({ unit_id: 'unit-a' }), onSaved: vi.fn() });
    const aIndex = candidateListIndex('unit-a');
    expect(aIndex).toBeGreaterThanOrEqual(0);

    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B' }), onSaved: vi.fn() });
    const bIndex = candidateListIndex('unit-b');
    expect(bIndex).toBeGreaterThanOrEqual(0);
    await respondTo(
      bIndex,
      200,
      pageOf([candidateFixture({ id: 'cand-b', unit_id: 'unit-b', result: candidateResult('B页候选文字') })]),
    );

    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('B页候选文字');

    // 旧页响应这时才回来，必须被丢弃
    await respondTo(
      aIndex,
      200,
      pageOf([candidateFixture({ id: 'cand-a', result: candidateResult('A页迟到候选') })]),
    );
    expect(harness.container.textContent).not.toContain('A页迟到候选');
    expect(harness.container.textContent).toContain('B页候选文字');
    harness.unmount();
  });

  const adoptionCases: Array<[string, Partial<OcrCandidateDetail>, number | undefined, boolean]> = [
    ['就绪且基础修订号与当前一致可採用', { state: 'ready', base_content_revision: 7 }, 7, true],
    ['就绪但基础修订号过期不可採用', { state: 'ready', base_content_revision: 6 }, 7, false],
    ['排队中不可採用', { state: 'queued' }, 7, false],
    ['识别中不可採用', { state: 'running' }, 7, false],
    ['失败不可採用', { state: 'failed' }, 7, false],
    ['陈旧不可採用', { state: 'stale' }, 7, false],
    ['已采用不可重复採用', { state: 'adopted' }, 7, false],
    ['已保留不可採用', { state: 'rejected' }, 7, false],
    ['缺少当前修订号时不可採用（不臆造 0）', { state: 'ready', base_content_revision: 0 }, undefined, false],
  ];

  it.each(adoptionCases)('%s', async (_label, candidateOverrides, currentRevision, expectedEnabled) => {
    const unit = unitFixture();
    if (currentRevision === undefined) {
      delete (unit as { content_revision?: number }).content_revision;
    } else {
      (unit as { content_revision?: number }).content_revision = currentRevision;
    }
    const harness = await mountPanel({ unit, onSaved: vi.fn() });
    const listIndex = candidateListIndex('unit-a');
    await respondTo(listIndex, 200, pageOf([candidateFixture(candidateOverrides)]));
    const adopt = buttonByText(harness.container, '采用候选');
    expect(adopt.disabled).toBe(!expectedEnabled);
    harness.unmount();
  });

  it('采用就绪候选：带当前修订号提交，采用后显示服务端返回的现用文字（校订保持现用）', async () => {
    const onSaved = vi.fn();
    const unit = unitFixture({
      text: '人工校订后的现用文字',
      corrected_text: '人工校订后的现用文字',
      version: 'corrected',
      original_text: '机器原文',
      content_revision: 7,
    });
    const harness = await mountPanel({ unit, onSaved });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('候选识别文字') })]),
    );

    await click(buttonByText(harness.container, '采用候选'));
    const adoptIndex = findCall('/adopt', 'POST');
    expect(adoptIndex).toBeGreaterThanOrEqual(0);
    expect(pending[adoptIndex].body).toEqual({ expected_content_revision: 7 });

    const returnedUnit = unitFixture({
      text: '人工校订后的现用文字',
      corrected_text: '人工校订后的现用文字',
      version: 'corrected',
      original_text: '候选识别文字',
      content_revision: 8,
    });
    await respondTo(adoptIndex, 200, {
      candidate: candidateFixture({ state: 'adopted', result: candidateResult('候选识别文字') }),
      unit: returnedUnit,
    });

    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(harness.container.textContent).toContain('人工校订后的现用文字');
    expect(harness.container.textContent).toContain('已采用');
    harness.unmount();
  });

  it('采用遇到 409：保留对照并要求重新核对，不自动重试', async () => {
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture({ content_revision: 7 }), onSaved });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('候选识别文字') })]),
    );
    await click(buttonByText(harness.container, '候选文字'));
    await click(buttonByText(harness.container, '采用候选'));

    const adoptIndex = findCall('/adopt', 'POST');
    await respondTo(adoptIndex, 409, {
      error: { code: 'CONTENT_REVISION_CONFLICT', message: '现用文字已变化，请重新核对后再采用。' },
    });
    await settle();

    expect(harness.container.textContent).toContain('重新核对');
    expect(harness.container.textContent).toContain('候选识别文字'); // 对照仍在
    expect(pending.filter((call) => call.url.includes('/adopt') && call.method === 'POST')).toHaveLength(1);
    expect(onSaved).not.toHaveBeenCalled();
    harness.unmount();
  });

  it('保留现用：拒绝候选后现用文字不变', async () => {
    const harness = await mountPanel({ unit: unitFixture({ text: '现用文字甲', content_revision: 7 }), onSaved: vi.fn() });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('候选文字乙') })]),
    );

    await click(buttonByText(harness.container, '保留现用'));
    const rejectIndex = findCall('/reject', 'POST');
    expect(rejectIndex).toBeGreaterThanOrEqual(0);
    expect(typeof (pending[rejectIndex].body as { reason?: unknown }).reason).toBe('string');
    await respondTo(
      rejectIndex,
      200,
      candidateFixture({ state: 'rejected', result: candidateResult('候选文字乙') }),
    );

    expect(harness.container.textContent).toContain('现用文字甲');
    expect(harness.container.textContent).toContain('已保留');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(true);
    harness.unmount();
  });

  it('区分现用文字 / 候选文字 / 旧 OCR 底稿', async () => {
    const unit = unitFixture({
      text: '现用文字甲',
      original_text: '旧底稿乙',
      corrected_text: null,
      version: 'original',
      content_revision: 7,
    });
    const harness = await mountPanel({ unit, onSaved: vi.fn() });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('候选文字丙') })]),
    );

    await click(buttonByText(harness.container, '现用文字'));
    expect(harness.container.textContent).toContain('现用文字甲');
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('候选文字丙');
    await click(buttonByText(harness.container, '旧 OCR 底稿'));
    expect(harness.container.textContent).toContain('旧底稿乙');
    harness.unmount();
  });

  it('差异开关标出替换差异，并说明差异不代表更正确', async () => {
    const unit = unitFixture({ text: '媒妁之言', original_text: null, content_revision: 7 });
    const harness = await mountPanel({ unit, onSaved: vi.fn() });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('媒约之言') })]),
    );
    await click(buttonByText(harness.container, '候选文字'));

    const toggle = harness.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(toggle).not.toBeNull();
    await click(toggle as HTMLInputElement);

    const marks = Array.from(harness.container.querySelectorAll('mark'));
    expect(marks.length).toBeGreaterThan(0);
    expect(marks.map((mark) => mark.textContent).join('')).toContain('约');
    expect(harness.container.textContent).toContain('不代表更正确');
    harness.unmount();
  });

  it('运行中的候选轮询在切换单元后停止写入，并在卸载时清理', async () => {
    vi.useFakeTimers();
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture({ unit_id: 'unit-a' }), onSaved });

    // 就绪前会即时轮询一次；反复响应运行中的候选直到不再产生新请求。
    for (let i = 0; i < 5; i += 1) {
      const index = candidateListIndex('unit-a');
      if (index < 0) break;
      await respondTo(index, 200, pageOf([candidateFixture({ state: 'running' })]));
    }
    const requestsForA = pending.filter((call) => call.url.includes('/units/unit-a/ocr-candidates')).length;
    expect(requestsForA).toBeGreaterThan(0);

    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B' }), onSaved });
    const bIndex = candidateListIndex('unit-b');
    await respondTo(
      bIndex,
      200,
      pageOf([candidateFixture({ id: 'cand-b', unit_id: 'unit-b', state: 'ready', base_content_revision: 7 })]),
    );

    const countAfterSwitch = pending.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await settle();
    expect(pending.length).toBe(countAfterSwitch);
    expect(pending.filter((call) => call.url.includes('/units/unit-a/ocr-candidates'))).toHaveLength(requestsForA);

    harness.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(pending.length).toBe(countAfterSwitch);
  });

  it('首次生成尚无候选行：任务进行中也持续轮询，直到发现首个候选', async () => {
    vi.useFakeTimers();
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-9' });
    expect(pending.length).toBeGreaterThan(0);

    const jobIndex = jobCallIndex('job-9');
    expect(jobIndex).toBeGreaterThanOrEqual(0);
    await respondTo(jobIndex, 200, jobFixture({ state: 'running' }));
    for (let i = 0; i < 5; i += 1) {
      const listIndex = candidateListIndex('unit-a');
      if (listIndex < 0) break;
      await respondTo(listIndex, 200, pageOf([]));
    }

    const afterBurst = pending.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await settle();
    // 列表仍为空时也必须继续轮询，否则永远发现不了首个候选行。
    expect(pending.length).toBeGreaterThan(afterBurst);
    expect(candidateListIndex('unit-a')).toBeGreaterThanOrEqual(0);

    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture()]));
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('媒约之言');
    harness.unmount();
  });

  it('列表只给摘要时按需取详情；取得完整候选前不可采用', async () => {
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture({ content_revision: 7 }), onSaved });

    const summary = candidateFixture();
    delete (summary as { result?: unknown }).result;
    delete (summary as { error?: unknown }).error;
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([summary]));

    // 摘要行没有 result：应先取详情，未取得前采用不可用。
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(true);
    const detailIndex = findCall('/ocr-candidates/cand-1');
    expect(detailIndex).toBeGreaterThanOrEqual(0);

    await respondTo(detailIndex, 200, candidateFixture());

    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('媒约之言');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(false);

    await click(buttonByText(harness.container, '采用候选'));
    const adoptIndex = findCall('/adopt', 'POST');
    expect(adoptIndex).toBeGreaterThanOrEqual(0);
    expect(pending[adoptIndex].body).toEqual({ expected_content_revision: 7 });
    await respondTo(adoptIndex, 200, {
      candidate: candidateFixture({ state: 'adopted' }),
      unit: unitFixture({ content_revision: 8 }),
    });
    expect(onSaved).toHaveBeenCalledTimes(1);
    harness.unmount();
  });

  it('采用返回前若已切换单元：不写回结果、不调用新单元的 onSaved', async () => {
    const onSavedA = vi.fn();
    const harness = await mountPanel({ unit: unitFixture({ unit_id: 'unit-a' }), onSaved: onSavedA });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture({ result: candidateResult('A候选') })]));
    await click(buttonByText(harness.container, '采用候选'));
    const adoptIndex = findCall('/adopt', 'POST');
    expect(adoptIndex).toBeGreaterThanOrEqual(0);

    const onSavedB = vi.fn();
    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B' }), onSaved: onSavedB });

    await respondTo(adoptIndex, 200, {
      candidate: candidateFixture({ state: 'adopted' }),
      unit: unitFixture({ unit_id: 'unit-a', text: 'A被采用后的现用文字' }),
    });

    expect(onSavedA).not.toHaveBeenCalled();
    expect(onSavedB).not.toHaveBeenCalled();
    await click(buttonByText(harness.container, '现用文字'));
    expect(harness.container.textContent).toContain('现用B');
    expect(harness.container.textContent).not.toContain('A被采用后的现用文字');
    harness.unmount();
  });

  it('保留返回前若已切换单元：不写回决定', async () => {
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a', text: '现用A', content_revision: 7 }),
      onSaved: vi.fn(),
    });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture({ result: candidateResult('A候选') })]));
    await click(buttonByText(harness.container, '保留现用'));
    const rejectIndex = findCall('/reject', 'POST');
    expect(rejectIndex).toBeGreaterThanOrEqual(0);

    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B' }), onSaved: vi.fn() });
    await respondTo(rejectIndex, 200, candidateFixture({ state: 'rejected', unit_id: 'unit-a' }));

    await click(buttonByText(harness.container, '现用文字'));
    expect(harness.container.textContent).toContain('现用B');
    expect(harness.container.textContent).not.toContain('已保留');
    harness.unmount();
  });

  it('切换单元后旧列表数据被忽略：不显示旧候选，采用保持禁用', async () => {
    const harness = await mountPanel({ unit: unitFixture({ unit_id: 'unit-a', content_revision: 7 }), onSaved: vi.fn() });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture({ result: candidateResult('A候选文字') })]));
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('A候选文字');

    // B 的列表还没回来：不能继续显示 A 的旧行，也不能启用采用。
    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B', content_revision: 7 }), onSaved: vi.fn() });
    expect(harness.container.textContent).not.toContain('A候选文字');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(true);
    harness.unmount();
  });

  it('空列表且任务已结束：最终刷新后停止轮询（不再无限刷新）', async () => {
    vi.useFakeTimers();
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-9' });

    // 任务结束：响应所有（含被中止的）任务请求。
    for (let i = 0; i < 6; i += 1) {
      const jobIndex = jobCallIndex('job-9');
      if (jobIndex < 0) break;
      await respondTo(jobIndex, 200, jobFixture({ state: 'completed' }));
    }

    // 排空列表，包含任务结束触发的一次「最终刷新」。
    for (let round = 0; round < 8; round += 1) {
      await settle();
      const listIndex = candidateListIndex('unit-a');
      if (listIndex < 0) continue;
      await respondTo(listIndex, 200, pageOf([]));
    }

    const before = listRequests;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await settle();
    for (let i = 0; i < 6; i += 1) {
      const listIndex = candidateListIndex('unit-a');
      if (listIndex < 0) break;
      await respondTo(listIndex, 200, pageOf([]));
    }
    // 空列表 + 已结束：推进时间不再产生新的列表请求。
    expect(listRequests).toBe(before);
    harness.unmount();
  });

  it('新排队任务列表为空也持续轮询，候选出现后展示', async () => {
    vi.useFakeTimers();
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-9' });
    await respondTo(jobCallIndex('job-9'), 200, jobFixture({ state: 'queued' }));
    for (let i = 0; i < 6; i += 1) {
      const listIndex = candidateListIndex('unit-a');
      if (listIndex < 0) break;
      await respondTo(listIndex, 200, pageOf([]));
    }

    const afterBurst = pending.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await settle();
    expect(pending.length).toBeGreaterThan(afterBurst);

    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture()]));
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('媒约之言');
    harness.unmount();
  });

  it('详情读取失败时不展示候选文字，也不允许采用', async () => {
    const harness = await mountPanel({ unit: unitFixture({ content_revision: 7 }), onSaved: vi.fn() });
    const summary = candidateFixture();
    delete (summary as { result?: unknown }).result;
    delete (summary as { error?: unknown }).error;
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([summary]));

    await respondTo(findCall('/ocr-candidates/cand-1'), 500, {
      error: { code: 'SERVER_ERROR', message: '详情读取失败，请稍后重试。' },
    });

    expect(buttonByText(harness.container, '采用候选').disabled).toBe(true);
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).not.toContain('媒约之言');
    expect(harness.container.textContent).toContain('详情读取失败');
    harness.unmount();
  });

  it('同任务有其它待核页时提供「下一待核页」并沿用现有导航', async () => {
    const onSelectUnit = vi.fn();
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a' }),
      onSaved: vi.fn(),
      jobId: 'job-9',
      onSelectUnit,
    });
    const unitsIndex = jobUnitsCallIndex('job-9');
    expect(unitsIndex).toBeGreaterThanOrEqual(0);
    await respondTo(unitsIndex, 200, {
      items: [jobUnit('unit-a', 'ready'), jobUnit('unit-c', 'ready'), jobUnit('unit-d', 'failed')],
      total: 3,
      page: 1,
      page_size: 100,
    });

    const next = buttonByText(harness.container, '下一待核页');
    expect(next.disabled).toBe(false);
    await click(next);
    expect(onSelectUnit).toHaveBeenCalledWith('unit-c');
    harness.unmount();
  });

  it('同任务只有当前页待核时「下一待核页」不可用', async () => {
    const onSelectUnit = vi.fn();
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a' }),
      onSaved: vi.fn(),
      jobId: 'job-9',
      onSelectUnit,
    });
    await respondTo(jobUnitsCallIndex('job-9'), 200, {
      items: [jobUnit('unit-a', 'ready')],
      total: 1,
      page: 1,
      page_size: 100,
    });

    expect(buttonByText(harness.container, '下一待核页').disabled).toBe(true);
    harness.unmount();
  });

  it('摘要 running→ready 时重新取详情，最终显示候选文字并可采用', async () => {
    vi.useFakeTimers();
    const harness = await mountPanel({ unit: unitFixture({ content_revision: 7 }), onSaved: vi.fn() });

    const runningSummary = candidateFixture({ state: 'running' });
    delete (runningSummary as { result?: unknown }).result;
    delete (runningSummary as { error?: unknown }).error;
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([runningSummary]));

    // 第一次详情：候选仍在 running，result 为 null。
    const firstDetail = findCall('/ocr-candidates/cand-1');
    expect(firstDetail).toBeGreaterThanOrEqual(0);
    await respondTo(firstDetail, 200, candidateFixture({ state: 'running', result: null }));
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(true);

    // 轮询到 ready 摘要（仍只给摘要）。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await settle();
    const readySummary = candidateFixture({ state: 'ready' });
    delete (readySummary as { result?: unknown }).result;
    delete (readySummary as { error?: unknown }).error;
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([readySummary]));

    // 详情必须重新取（键含 state），取回 ready 的完整结果。
    const secondDetail = findCall('/ocr-candidates/cand-1');
    expect(secondDetail).toBeGreaterThanOrEqual(0);
    await respondTo(secondDetail, 200, candidateFixture({ state: 'ready', result: candidateResult('媒约之言，父母之命。') }));

    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('媒约之言');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(false);
    harness.unmount();
  });

  it('A→B→A 后第一个 A 的迟到采用结果不写回', async () => {
    const onSavedA = vi.fn();
    const harness = await mountPanel({ unit: unitFixture({ unit_id: 'unit-a' }), onSaved: onSavedA });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ result: candidateResult('A候选') })]),
    );
    await click(buttonByText(harness.container, '采用候选'));
    const adoptIndex = findCall('/adopt', 'POST');
    expect(adoptIndex).toBeGreaterThanOrEqual(0);

    // 切到 B 再切回 A：同一 unit_id 也不能让第一个 A 的迟到结果落回。
    await harness.rerender({ unit: unitFixture({ unit_id: 'unit-b', text: '现用B' }), onSaved: vi.fn() });
    await harness.rerender({
      unit: unitFixture({ unit_id: 'unit-a', text: '现用A', content_revision: 7 }),
      onSaved: onSavedA,
    });

    await respondTo(adoptIndex, 200, {
      candidate: candidateFixture({ state: 'adopted' }),
      unit: unitFixture({ unit_id: 'unit-a', text: 'A被采用后的现用文字' }),
    });

    expect(onSavedA).not.toHaveBeenCalled();
    await click(buttonByText(harness.container, '现用文字'));
    expect(harness.container.textContent).toContain('现用A');
    expect(harness.container.textContent).not.toContain('A被采用后的现用文字');
    harness.unmount();
  });

  it('未显式传入 jobId 时按候选的 job_id 提供「下一待核页」', async () => {
    const onSelectUnit = vi.fn();
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a' }),
      onSaved: vi.fn(),
      onSelectUnit,
    });
    await respondTo(
      candidateListIndex('unit-a'),
      200,
      pageOf([candidateFixture({ job_id: 'job-77', result: candidateResult('候选') })]),
    );

    const unitsIndex = jobUnitsCallIndex('job-77');
    expect(unitsIndex).toBeGreaterThanOrEqual(0);
    await respondTo(unitsIndex, 200, {
      items: [jobUnit('unit-a', 'ready'), jobUnit('unit-x', 'ready')],
      total: 2,
      page: 1,
      page_size: 100,
    });

    const next = buttonByText(harness.container, '下一待核页');
    expect(next.disabled).toBe(false);
    await click(next);
    expect(onSelectUnit).toHaveBeenCalledWith('unit-x');
    harness.unmount();
  });

  it('任务结束（而非只在排队时）刷新「下一待核页」', async () => {
    vi.useFakeTimers();
    const onSelectUnit = vi.fn();
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a' }),
      onSaved: vi.fn(),
      jobId: 'job-9',
      onSelectUnit,
    });

    // 第一次扫描：任务还在排队，其它页尚无 ready。
    await respondTo(jobUnitsCallIndex('job-9'), 200, {
      items: [jobUnit('unit-a', 'ready'), jobUnit('unit-c', null)],
      total: 2,
      page: 1,
      page_size: 100,
    });
    expect(buttonByText(harness.container, '下一待核页').disabled).toBe(true);

    // 任务结束：应触发重新扫描。
    for (let i = 0; i < 4; i += 1) {
      const jobIndex = jobCallIndex('job-9');
      if (jobIndex < 0) break;
      await respondTo(jobIndex, 200, jobFixture({ state: 'completed' }));
    }

    const rescan = jobUnitsCallIndex('job-9');
    expect(rescan).toBeGreaterThanOrEqual(0);
    await respondTo(rescan, 200, {
      items: [jobUnit('unit-a', 'ready'), jobUnit('unit-c', 'ready')],
      total: 2,
      page: 1,
      page_size: 100,
    });
    expect(buttonByText(harness.container, '下一待核页').disabled).toBe(false);
    harness.unmount();
  });

  it('待核页扫描失败不冒充没有待核页且可重试', async () => {
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-9', onSelectUnit: vi.fn() });
    await respondTo(jobUnitsCallIndex('job-9'), 500, {
      error: { code: 'INTERNAL', message: '待核页列表暂不可读' },
    });
    expect(harness.container.textContent).toContain('待核页列表暂不可读');
    expect(harness.container.textContent).not.toContain('暂无其它待核对页');
    await click(buttonByText(harness.container, '重试待核页'));
    expect(jobUnitsCallIndex('job-9')).toBeGreaterThanOrEqual(0);
    harness.unmount();
  });

  it('详情展示真实模型参数和资源记录', async () => {
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn() });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture({
      result: { ...candidateResult('候选')!, model_fingerprints: [{ filename: 'real-rec.onnx', sha256: 'actual-model-sha' }],
        config: { rec_batch: 1 }, timing: { total_seconds: 3.75 }, memory: { peak_private_bytes: 123456789 } },
    })]));
    expect(harness.container.textContent).toContain('real-rec.onnx');
    expect(harness.container.textContent).toContain('actual-model-sha');
    expect(harness.container.textContent).toContain('rec_batch');
    expect(harness.container.textContent).toContain('3.75');
    expect(harness.container.textContent).toContain('123456789');
    harness.unmount();
  });

  it('候选列表读取失败显示原因并可重新读取', async () => {
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn() });
    await respondTo(candidateListIndex('unit-a'), 500, {
      error: { code: 'INTERNAL', message: '资料库暂时不可读' },
    });
    expect(harness.container.textContent).toContain('资料库暂时不可读');
    await click(buttonByText(harness.container, '重试读取'));
    expect(candidateListIndex('unit-a')).toBeGreaterThanOrEqual(0);
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture()]));
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(false);
    harness.unmount();
  });

  it('先看到运行中候选再收到任务完成时最终刷新候选', async () => {
    vi.useFakeTimers();
    const harness = await mountPanel({ unit: unitFixture(), onSaved: vi.fn(), jobId: 'job-9' });
    for (let i = 0; i < 8 && candidateListIndex('unit-a') >= 0; i += 1) {
      await respondTo(candidateListIndex('unit-a'), 200, pageOf([
        candidateFixture({ job_id: 'job-9', state: 'running', result: null }),
      ]));
    }
    for (let i = 0; i < 8 && jobCallIndex('job-9') >= 0; i += 1) {
      await respondTo(jobCallIndex('job-9'), 200, jobFixture({ id: 'job-9', state: 'completed' }));
    }
    const finalList = candidateListIndex('unit-a');
    expect(finalList).toBeGreaterThanOrEqual(0);
    await respondTo(finalList, 200, pageOf([
      candidateFixture({ job_id: 'job-9', result: candidateResult('任务完成后的候选全文') }),
    ]));
    expect(harness.container.textContent).toContain('任务完成后的候选全文');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(false);
    harness.unmount();
  });

  it('同页保留旧候选后新建任务仍显示新候选', async () => {
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture(), onSaved, jobId: 'job-1' });
    for (let i = 0; i < 8 && candidateListIndex('unit-a') >= 0; i += 1) {
      await respondTo(candidateListIndex('unit-a'), 200, pageOf([candidateFixture()]));
    }
    for (let i = 0; i < 8 && jobCallIndex('job-1') >= 0; i += 1) {
      await respondTo(jobCallIndex('job-1'), 200, jobFixture({ id: 'job-1', state: 'completed' }));
    }
    await click(buttonByText(harness.container, '保留现用'));
    await respondTo(findCall('/reject', 'POST'), 200, candidateFixture({ state: 'rejected' }));
    await harness.rerender({ unit: unitFixture(), onSaved, jobId: 'job-2' });
    for (let i = 0; i < 8 && candidateListIndex('unit-a') >= 0; i += 1) {
      await respondTo(candidateListIndex('unit-a'), 200, pageOf([
        candidateFixture({ id: 'cand-2', job_id: 'job-2', result: candidateResult('第二次生成的新候选') }),
      ]));
    }
    await click(buttonByText(harness.container, '候选文字'));
    expect(harness.container.textContent).toContain('第二次生成的新候选');
    expect(buttonByText(harness.container, '采用候选').disabled).toBe(false);
    harness.unmount();
  });

  it('旧任务终结状态不会停止同页新任务的轮询', async () => {
    vi.useFakeTimers();
    const onSaved = vi.fn();
    const harness = await mountPanel({ unit: unitFixture(), onSaved, jobId: 'job-1' });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([]));
    for (let i = 0; i < 8 && jobCallIndex('job-1') >= 0; i += 1) {
      await respondTo(jobCallIndex('job-1'), 200, jobFixture({ id: 'job-1', state: 'completed' }));
    }
    // 消化旧任务结束时的最终列表刷新，留下新任务的明确请求。
    for (let i = 0; i < 4 && candidateListIndex('unit-a') >= 0; i += 1) {
      await respondTo(candidateListIndex('unit-a'), 200, pageOf([]));
    }
    await harness.rerender({ unit: unitFixture(), onSaved, jobId: 'job-2' });
    await respondTo(candidateListIndex('unit-a'), 200, pageOf([]));
    for (let i = 0; i < 8 && jobCallIndex('job-2') >= 0; i += 1) {
      await respondTo(jobCallIndex('job-2'), 200, jobFixture({ id: 'job-2', state: 'queued' }));
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    await settle();
    expect(jobCallIndex('job-2')).toBeGreaterThanOrEqual(0);
    harness.unmount();
  });

  it('采用当前页后重新扫描「下一待核页」', async () => {
    const onSelectUnit = vi.fn();
    const harness = await mountPanel({
      unit: unitFixture({ unit_id: 'unit-a', content_revision: 7 }),
      onSaved: vi.fn(),
      jobId: 'job-9',
      onSelectUnit,
    });

    await respondTo(jobUnitsCallIndex('job-9'), 200, {
      items: [jobUnit('unit-a', 'ready')],
      total: 1,
      page: 1,
      page_size: 100,
    });
    expect(buttonByText(harness.container, '下一待核页').disabled).toBe(true);

    // 有 jobId 时列表会即时轮询，可能有多个待响应请求：全部以就绪候选作答。
    for (let i = 0; i < 4; i += 1) {
      const listIndex = candidateListIndex('unit-a');
      if (listIndex < 0) break;
      await respondTo(listIndex, 200, pageOf([candidateFixture({ result: candidateResult('候选') })]));
    }
    await click(buttonByText(harness.container, '采用候选'));
    await respondTo(findCall('/adopt', 'POST'), 200, {
      candidate: candidateFixture({ state: 'adopted' }),
      unit: unitFixture({ content_revision: 8 }),
    });

    // 采用后决定变化会触发重新扫描；可能有被中止的旧请求，全部以“其它页就绪”作答。
    for (let i = 0; i < 4; i += 1) {
      const rescan = jobUnitsCallIndex('job-9');
      if (rescan < 0) break;
      await respondTo(rescan, 200, {
        items: [jobUnit('unit-a', 'adopted'), jobUnit('unit-d', 'ready')],
        total: 2,
        page: 1,
        page_size: 100,
      });
    }
    expect(buttonByText(harness.container, '下一待核页').disabled).toBe(false);
    harness.unmount();
  });
});
