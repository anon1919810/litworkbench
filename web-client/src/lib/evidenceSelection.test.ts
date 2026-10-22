/**
 * 摘录选择与导出范围的纯逻辑回归。
 *
 * 这几条是「选择/范围悄悄变大」类事故的防线：
 * 翻页必须保留选择，换筛选必须清空并解释，列表在途时不能批量操作，
 * 预览令牌一旦过期就不能再生成文件。
 */
import { describe, expect, it } from 'vitest';
import {
  bulkAvailability,
  clearSelection,
  createSelection,
  describeScope,
  emptyScope,
  isSelected,
  reconcileSelection,
  scopeKey,
  selectedOnPage,
  setPageSelection,
  toggleOne,
} from './evidenceSelection';
import {
  buildExportRequest,
  canGenerate,
  currentKey,
  describeExportScope,
  describeReviewCounts,
  exportReducer,
  generateDisabledReason,
  initialExportState,
  previewValid,
} from './exportScope';
import type { ExportDialogState } from './exportScope';
import type { ExportPreview } from '../api/types';
import type { EvidenceScope } from './evidenceSelection';

const ALL: EvidenceScope = emptyScope();

function preview(token: string, count = 65): ExportPreview {
  return {
    format: 'csv',
    filters: { review_status: 'accepted' },
    count,
    review_counts: { pending: 0, accepted: count, rejected: 0, stale: 0 },
    snapshot_token: token,
    filename: `摘录资料_${token.slice(0, 12)}.csv`,
    scope_description: '范围：当前筛选的全部结果；核验状态：仅已接受（默认）',
    review_policy: 'accepted',
    material_note: '本文件是摘录资料集，不包含研究结论。',
    review_note: '已接受只表示摘录被人工核验过。',
  };
}

/* ------------------------------- 选择 ------------------------------- */

describe('跨页选择', () => {
  it('翻页不清空选择，本页全选只动本页', () => {
    const scope = scopeKey(ALL);
    let state = createSelection(scope);
    state = toggleOne(state, 'ev_1');
    state = setPageSelection(state, ['ev_2', 'ev_3'], true);
    expect(state.ids).toEqual(['ev_1', 'ev_2', 'ev_3']);
    expect(selectedOnPage(state, ['ev_2', 'ev_3'])).toBe(2);

    // 第 2 页：只影响第 2 页
    state = setPageSelection(state, ['ev_9'], true);
    expect(state.ids).toContain('ev_1');
    expect(state.ids).toContain('ev_9');
    state = setPageSelection(state, ['ev_2', 'ev_3'], false);
    expect(state.ids).toEqual(['ev_1', 'ev_9']);
    expect(isSelected(state, 'ev_1')).toBe(true);
  });

  it('切换同一页上的某一条不会影响其它条目', () => {
    let state = createSelection(scopeKey(ALL));
    state = toggleOne(state, 'ev_a');
    state = toggleOne(state, 'ev_b');
    state = toggleOne(state, 'ev_a'); // 取消
    expect(state.ids).toEqual(['ev_b']);
  });

  it('清空选择后范围标记也清掉', () => {
    const state = clearSelection();
    expect(state.ids).toEqual([]);
    expect(state.scope).toBe('');
  });
});

describe('筛选变化时清空选择并解释', () => {
  const base = { ...ALL };
  const withCategory: EvidenceScope = { ...ALL, category: '文物' };

  it('范围没变（只是翻页）时原样保留', () => {
    let state = createSelection(scopeKey(base));
    state = toggleOne(state, 'ev_1');
    const result = reconcileSelection(state, scopeKey(base));
    expect(result.next.ids).toEqual(['ev_1']);
    expect(result.notice).toBeNull();
  });

  it('范围变了就清空，并说明清了多少条', () => {
    let state = createSelection(scopeKey(base));
    state = toggleOne(state, 'ev_1');
    state = toggleOne(state, 'ev_2');
    const result = reconcileSelection(state, scopeKey(withCategory));
    expect(result.next.ids).toEqual([]);
    expect(result.next.scope).toBe(scopeKey(withCategory));
    expect(result.notice).toContain('2');
    expect(result.notice).toContain('分类 文物');
  });

  it('页码不参与范围键，所以翻页不会被误判为换筛选', () => {
    expect(scopeKey({ ...ALL })).toBe(scopeKey({ ...ALL }));
    expect(scopeKey(withCategory)).not.toBe(scopeKey(base));
  });

  it('范围说明覆盖四类筛选条件', () => {
    expect(describeScope(ALL)).toBe('全部摘录');
    expect(
      describeScope({ docId: 'd1', category: '文物', collectionId: 'c1', reviewStatus: 'accepted' }),
    ).toBe('来源文献、分类 文物、集合、核验状态');
  });
});

describe('列表在途时禁止批量操作', () => {
  it('stale（旧行还在、请求未回）时禁用并解释', () => {
    const selection = { ids: ['ev_1'], scope: 'k' };
    const result = bulkAvailability(selection, true, true);
    expect(result.enabled).toBe(false);
    expect(result.reason).toContain('重新取数');
  });

  it('范围一致时可用', () => {
    const selection = { ids: ['ev_1'], scope: 'k' };
    expect(bulkAvailability(selection, false, false).enabled).toBe(true);
  });

  it('一条没选时不可用', () => {
    const result = bulkAvailability({ ids: [], scope: 'k' }, false, false);
    expect(result.enabled).toBe(false);
    expect(result.reason).toContain('勾选');
  });
});

/* ------------------------------- 导出 ------------------------------- */

describe('导出请求体', () => {
  it('默认只导出已接受，不带任何路径或文件名', () => {
    const state = initialExportState(ALL, [], 65, false);
    const request = buildExportRequest(state)!;
    expect(request.filters.review_status).toBe('accepted');
    expect(request.evidence_ids).toBeUndefined();
    expect(JSON.stringify(request)).not.toContain('path');
    expect(JSON.stringify(request)).not.toContain('.csv');
  });

  it('显式选全部状态才会带上其它状态', () => {
    let state = initialExportState(ALL, [], 65, false);
    state = exportReducer(state, { type: 'set-review', review: 'all' });
    expect(buildExportRequest(state)!.filters.review_status).toBe('all');
  });

  it('列表筛选条件会进入导出范围', () => {
    const scope: EvidenceScope = { docId: 'd1', category: '文物', collectionId: 'c1',
                                   reviewStatus: 'stale' };
    const state = initialExportState(scope, [], 10, false);
    const request = buildExportRequest(state)!;
    expect(request.filters).toMatchObject({
      doc_id: 'd1', category: '文物', collection_id: 'c1', review_status: 'accepted',
    });
  });

  it('选了模式但一条没选时不发空数组', () => {
    let state = initialExportState(ALL, ['ev_1'], 65, false);
    state = exportReducer(state, { type: 'set-selected', ids: [] });
    expect(state.mode).toBe('selected');
    expect(buildExportRequest(state)).toBeNull();
    expect(state.notice).toContain('已选 0 条');
  });

  it('已选模式带上显式 ID', () => {
    const state = initialExportState(ALL, ['ev_1', 'ev_2'], 65, false);
    expect(buildExportRequest(state)!.evidence_ids).toEqual(['ev_1', 'ev_2']);
  });

  it('范围说明区分已选条目与全部页，并明确核验口径', () => {
    expect(describeExportScope(initialExportState(ALL, ['a', 'b'], 65, false)))
      .toContain('已选 2 条');
    const filtered = exportReducer(initialExportState(ALL, ['a', 'b'], 65, false),
                                   { type: 'set-mode', mode: 'filtered' });
    expect(describeExportScope(filtered)).toContain('导出包含所有页');
    expect(describeExportScope(filtered)).toContain('以核验状态和预览为准');
    expect(describeExportScope(filtered)).toContain('65');
  });
});

describe('预览令牌与迟到的响应', () => {
  const ready = (): ExportDialogState => {
    let state = initialExportState(ALL, [], 65, false);
    const key = currentKey(state);
    state = exportReducer(state, { type: 'preview-request', key });
    return exportReducer(state, { type: 'preview-ok', key, preview: preview('a'.repeat(64)) });
  };

  it('预览完成后可以生成', () => {
    const state = ready();
    expect(previewValid(state)).toBe(true);
    expect(canGenerate(state)).toBe(true);
    expect(generateDisabledReason(state)).toBeNull();
  });

  it('预览请求在途时不能生成', () => {
    const state = initialExportState(ALL, [], 65, false);
    const key = currentKey(state);
    const pending = exportReducer(state, { type: 'preview-request', key });
    expect(canGenerate(pending)).toBe(false);
    expect(generateDisabledReason(pending)).toContain('预览');
  });

  it('改格式后旧令牌立刻失效', () => {
    const state = exportReducer(ready(), { type: 'set-format', format: 'docx' });
    expect(state.preview).toBeNull();
    expect(state.previewKey).toBeNull();
    expect(canGenerate(state)).toBe(false);
    expect(generateDisabledReason(state)).toContain('预览');
  });

  it('改核验口径后旧令牌立刻失效', () => {
    const state = exportReducer(ready(), { type: 'set-review', review: 'all' });
    expect(canGenerate(state)).toBe(false);
    expect(state.previewKey).toBeNull();
  });

  it('切到已选模式后旧令牌失效（范围变了）', () => {
    const state = exportReducer(ready(), { type: 'set-mode', mode: 'selected' });
    expect(canGenerate(state)).toBe(false);
  });

  it('迟到的预览响应不会把「生成」重新点亮', () => {
    let state = initialExportState(ALL, [], 65, false);
    const staleKey = currentKey(state);
    state = exportReducer(state, { type: 'preview-request', key: staleKey });
    // 条件在请求途中变了
    state = exportReducer(state, { type: 'set-format', format: 'markdown' });
    // 旧响应这时才回来
    state = exportReducer(state, {
      type: 'preview-ok', key: staleKey, preview: preview('b'.repeat(64)),
    });
    expect(state.preview).toBeNull();
    expect(state.previewKey).toBeNull();
    expect(canGenerate(state)).toBe(false);
    expect(state.notice).toContain('过期');
  });

  it('迟到的失败响应不会覆盖新条件的错误提示', () => {
    let state = initialExportState(ALL, [], 65, false);
    const staleKey = currentKey(state);
    state = exportReducer(state, { type: 'preview-request', key: staleKey });
    state = exportReducer(state, { type: 'set-review', review: 'all' });
    state = exportReducer(state, { type: 'preview-fail', key: staleKey, message: '旧范围的错误' });
    expect(state.previewError).not.toContain('旧范围的错误');
    expect(state.previewError).toContain('过期');
  });

  it('409 保留设置、清空令牌并说明需要重新预览', () => {
    let state = ready();
    state = exportReducer(state, { type: 'set-selected', ids: ['ev_1'] });
    const key = currentKey(state);
    state = exportReducer(state, { type: 'preview-request', key });
    state = exportReducer(state, { type: 'preview-ok', key, preview: preview('c'.repeat(64)) });
    state = exportReducer(state, { type: 'generate-request' });
    state = exportReducer(state, { type: 'generate-conflict' });
    expect(state.format).toBe('csv');                 // 设置保留
    expect(state.selectedIds).toEqual(['ev_1']);
    expect(state.review).toBe('accepted');
    expect(state.preview).toBeNull();
    expect(canGenerate(state)).toBe(false);
    expect(state.notice).toContain('重新预览');
  });

  it('生成成功后记录结果，失败时保留错误文本', () => {
    let state = ready();
    state = exportReducer(state, { type: 'generate-request' });
    expect(state.generating).toBe(true);
    state = exportReducer(state, {
      type: 'generate-ok',
      record: {
        id: 'exp_1', format: 'csv', filename: '摘录资料_x.csv', count: 65,
        review_counts: { pending: 0, accepted: 65, rejected: 0, stale: 0 },
        download_url: '/api/v1/exports/exp_1/download', sha256: 'f'.repeat(64), byte_size: 10,
        snapshot_token: 'a'.repeat(64), scope: {}, created_at: '2026-10-15T00:00:00+00:00',
      },
    });
    expect(state.generating).toBe(false);
    expect(state.download?.id).toBe('exp_1');

    let failing = ready();
    failing = exportReducer(failing, { type: 'generate-request' });
    failing = exportReducer(failing, { type: 'generate-fail', message: '磁盘写入失败' });
    expect(failing.downloadError).toBe('磁盘写入失败');
    expect(failing.generating).toBe(false);
  });

  it('0 条预览不能生成', () => {
    let state = initialExportState(ALL, [], 0, false);
    const key = currentKey(state);
    state = exportReducer(state, { type: 'preview-request', key });
    state = exportReducer(state, { type: 'preview-ok', key, preview: preview('d'.repeat(64), 0) });
    expect(canGenerate(state)).toBe(false);
    expect(generateDisabledReason(state)).toContain('没有可导出的摘录');
  });

  it('列表在按新条件加载时不能导出「当前筛选的全部结果」', () => {
    const state = initialExportState(ALL, [], 65, true);
    expect(generateDisabledReason(state)).toContain('重新取数');
  });

  it('范围键随条件变化，用于判断令牌是否过期', () => {
    const state = initialExportState(ALL, [], 65, false);
    const base = currentKey(state);
    expect(currentKey(exportReducer(state, { type: 'set-format', format: 'json' }))).not.toBe(base);
    expect(currentKey(exportReducer(state, { type: 'set-review', review: 'all' }))).not.toBe(base);
    expect(currentKey(state)).toBe(base);
  });
});

describe('预览状态统计', () => {
  it('只显示真实存在的状态，用服务端给的中文标签', () => {
    expect(describeReviewCounts({ accepted: 33, pending: 0, rejected: 11, stale: 10 }))
      .toBe('已接受 33 · 已拒绝 11 · 需复核 10');
    expect(describeReviewCounts({ pending: 2, accepted: 0, rejected: 0, stale: 0 }))
      .toBe('待核验 2');
  });

  it('全为 0 或没有统计时给出可读提示', () => {
    expect(describeReviewCounts(undefined)).toBe('（没有状态统计）');
    expect(describeReviewCounts({})).toBe('0 条');
    expect(describeReviewCounts({ accepted: 0, pending: 0, rejected: 0, stale: 0 })).toBe('0 条');
  });
});
