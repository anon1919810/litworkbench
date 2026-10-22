/**
 * 导出对话框的纯状态机。
 *
 * 要防的错误只有一个：**拿过期的预览去生成文件**。
 * 因此预览结果必须和「它是为哪一组条件取的」绑在一起：
 *
 * * 范围/格式/核验口径任一变化 → 令牌立刻作废，「生成」按钮回到禁用；
 * * 迟到的预览响应（请求发出后条件又变了）被丢弃，不会把按钮重新点亮；
 * * 409（内容在预览之后变了）→ 保留设置、清空令牌，等重新预览。
 *
 * 默认只导出「已接受」；显式选「全部核验状态」才会带上待核验/需复核/已拒绝。
 * 前端永远不发送文件名或服务器路径。
 */
import type { ExportFormat, ExportPreview, ExportRecord, ExportRequest } from '../api/types';
import type { EvidenceScope } from './evidenceSelection';
import { num, reviewStatusInfo } from './format';

export type { ExportFormat, ExportPreview, ExportRecord, ExportRequest };

export type ExportMode = 'selected' | 'filtered';
export type ExportReviewScope = 'accepted' | 'all';

export interface ExportDialogState {
  mode: ExportMode;
  format: ExportFormat;
  review: ExportReviewScope;
  selectedIds: string[];
  scope: EvidenceScope;
  /** 列表正在按新条件加载：此时「当前筛选的全部结果」不可用 */
  listStale: boolean;
  /** 当前列表匹配的总条数（来自服务端，不是前端推算） */
  listTotal: number | null;

  preview: ExportPreview | null;
  /** 当前这组条件的预览令牌；与 previewKey 不一致时一律视为无预览 */
  previewKey: string | null;
  previewPending: boolean;
  previewError: string | null;

  generating: boolean;
  download: ExportRecord | null;
  downloadError: string | null;
  notice: string | null;
}

export function initialExportState(scope: EvidenceScope, selectedIds: string[],
                                  listTotal: number | null, listStale = false): ExportDialogState {
  return {
    mode: selectedIds.length ? 'selected' : 'filtered',
    format: 'csv',
    review: 'accepted',
    selectedIds,
    scope,
    listStale,
    listTotal,
    preview: null,
    previewKey: null,
    previewPending: false,
    previewError: null,
    generating: false,
    download: null,
    downloadError: null,
    notice: null,
  };
}

/**
 * 请求体。**不包含**任何路径或文件名：文件名与内容摘要由服务端决定。
 * 选了但一条没选时返回 null（空数组会被服务端拒绝，也不该发出去）。
 */
export function buildExportRequest(state: ExportDialogState): ExportRequest | null {
  const filters: ExportRequest['filters'] = {};
  if (state.scope.docId.trim()) filters.doc_id = state.scope.docId.trim();
  if (state.scope.category.trim()) filters.category = state.scope.category.trim();
  if (state.scope.collectionId.trim()) filters.collection_id = state.scope.collectionId.trim();
  filters.review_status = state.review;
  if (state.mode === 'selected') {
    if (state.selectedIds.length === 0) return null;
    return { format: state.format, filters, evidence_ids: [...state.selectedIds] };
  }
  return { format: state.format, filters };
}

/** 范围键：预览令牌只对它对应的那一个 key 有效。 */
export function requestKey(request: ExportRequest | null): string {
  if (!request) return '∅';
  return JSON.stringify({
    format: request.format,
    filters: Object.entries(request.filters).sort(([a], [b]) => (a < b ? -1 : 1)),
    ids: request.evidence_ids ?? null,
  });
}

export function currentKey(state: ExportDialogState): string {
  return requestKey(buildExportRequest(state));
}

/** 预览是否仍然有效（可以生成）。 */
export function previewValid(state: ExportDialogState): boolean {
  return Boolean(state.preview) && state.previewKey === currentKey(state) && !state.previewPending;
}

export type ExportAction =
  | { type: 'set-mode'; mode: ExportMode }
  | { type: 'set-format'; format: ExportFormat }
  | { type: 'set-review'; review: ExportReviewScope }
  | { type: 'set-selected'; ids: string[] }
  | { type: 'scope-changed'; scope: EvidenceScope; listTotal: number | null; listStale: boolean }
  | { type: 'preview-request'; key: string }
  | { type: 'preview-ok'; key: string; preview: ExportPreview }
  | { type: 'preview-fail'; key: string; message: string }
  | { type: 'generate-request' }
  | { type: 'generate-ok'; record: ExportRecord }
  | { type: 'generate-conflict' }
  | { type: 'generate-fail'; message: string };

/** 条件一变就作废令牌与下载结果；迟到的响应也不会把它们带回来。 */
function invalidate(state: ExportDialogState, notice: string | null): ExportDialogState {
  return { ...state, preview: null, previewKey: null, previewPending: false,
           previewError: null, download: null, downloadError: null, notice };
}

export function exportReducer(state: ExportDialogState, action: ExportAction): ExportDialogState {
  switch (action.type) {
    case 'set-mode': {
      if (action.mode === state.mode) return state;
      const next = { ...state, mode: action.mode };
      const request = buildExportRequest(next);
      if (!request) return { ...next, preview: null, previewKey: null, previewPending: false,
                              download: null, notice: '已选 0 条：请先勾选要导出的摘录，或改为导出当前筛选的全部结果。' };
      return invalidate(next, null);
    }
    case 'set-format':
      return action.format === state.format ? state : invalidate({ ...state, format: action.format }, null);
    case 'set-review':
      return action.review === state.review ? state : invalidate({ ...state, review: action.review }, null);
    case 'set-selected': {
      const next = { ...state, selectedIds: action.ids };
      if (state.mode !== 'selected') return next;
      return action.ids.length ? invalidate(next, null)
        : { ...invalidate(next, null), notice: '已选 0 条：请先勾选要导出的摘录，或改为导出当前筛选的全部结果。' };
    }
    case 'scope-changed':
      return invalidate({ ...state, scope: action.scope, listTotal: action.listTotal,
                          listStale: action.listStale }, null);
    case 'preview-request':
      return { ...state, previewPending: true, previewError: null,
               previewKey: action.key, preview: null, download: null, downloadError: null };
    case 'preview-ok': {
      if (action.key !== currentKey(state)) {
        // 迟到响应：范围/格式已经变了，丢弃，绝不把「生成」重新点亮
        return { ...state, previewPending: false, preview: null, previewKey: null,
                 download: null, downloadError: null,
                 notice: '预览结果已过期（导出条件在预览之后发生了变化），请重新预览。' };
      }
      return { ...state, previewPending: false, preview: action.preview, previewKey: action.key,
               previewError: null, notice: null };
    }
    case 'preview-fail':
      return { ...state, previewPending: false, preview: null, previewKey: null,
               previewError: action.key === currentKey(state) ? action.message
                 : '预览已过期，请重新预览。' };
    case 'generate-request':
      return previewValid(state) ? { ...state, generating: true, downloadError: null, notice: null }
                                 : state;
    case 'generate-ok':
      return { ...state, generating: false, download: action.record, downloadError: null, notice: null };
    case 'generate-conflict':
      // 409：内容变了。保留用户的范围/格式/核验设置，只清掉旧令牌。
      return { ...state, generating: false, preview: null, previewKey: null, previewPending: false,
               download: null,
               notice: '预览之后内容发生了变化（校订、审核或书目改动），已拒绝生成旧结果。请重新预览。' };
    case 'generate-fail':
      return { ...state, generating: false, downloadError: action.message };
    default:
      return state;
  }
}

/** 导出范围的说明文字：明确是「已选 N 条」还是「当前筛选的全部结果」。 */
export function describeExportScope(state: ExportDialogState): string {
  if (state.mode === 'selected') {
    return `已选 ${num(state.selectedIds.length)} 条摘录，实际导出数量以预览为准。`;
  }
  const total = state.listTotal === null ? '未知' : num(state.listTotal);
  return `当前列表共 ${total} 条；导出包含所有页，实际数量以核验状态和预览为准。`;
}

/** 「生成」按钮是否可以点。 */
export function canGenerate(state: ExportDialogState): boolean {
  if (!previewValid(state) || state.generating) return false;
  const preview = state.preview;
  if (!preview) return false;
  if (preview.count === 0) return false;
  return true;
}

export function generateDisabledReason(state: ExportDialogState): string | null {
  if (state.listStale && state.mode === 'filtered') {
    return '正在按新的筛选条件重新取数，请等列表加载完成后再导出当前筛选的全部结果。';
  }
  if (state.mode === 'selected' && state.selectedIds.length === 0) {
    return '已选 0 条：请先勾选要导出的摘录，或改为导出当前筛选的全部结果。';
  }
  if (state.previewPending) return '正在预览…';
  if (!state.preview) return '请先预览，确认条数与核验状态后再生成。';
  if (state.preview.count === 0) return '当前范围没有可导出的摘录。';
  if (state.generating) return '正在生成…';
  return null;
}

/** 预览面板上的状态统计：逐条保留服务端给出的真实分布。 */
export function describeReviewCounts(counts: Record<string, number> | undefined): string {
  if (!counts) return '（没有状态统计）';
  const parts = Object.entries(counts)
    .filter(([, value]) => value > 0)
    .map(([status, value]) => `${reviewStatusInfo(status).label} ${num(value)}`);
  return parts.length ? parts.join(' · ') : '0 条';
}

export const EXPORT_FORMATS: Array<{ value: ExportFormat; label: string }> = [
  { value: 'csv', label: 'CSV 表格（Excel 可直接打开）' },
  { value: 'json', label: 'JSON 数据（结构化）' },
  { value: 'markdown', label: 'Markdown 文本' },
  { value: 'docx', label: 'Word 文档' },
];
