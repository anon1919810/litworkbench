/**
 * 摘录选择与「当前筛选范围」的纯逻辑。
 *
 * 两条容易出错、必须在这里定死的规则：
 *
 * 1. **翻页不清空**：选择是跨页的，翻到第 3 页再回来，勾选还在。
 * 2. **换筛选必清空**：文献/分类/集合/核验状态任一变化后，原来的选择
 *    已经不对应屏幕上的结果了。此时清空选择并给出解释，而不是
 *    把旧选择悄悄带进「当前筛选的全部结果」导出。
 *
 * 范围键（``scopeKey``）只由筛选条件决定，**不含页码**——页码变化不是范围变化。
 */

/** 摘录列表的筛选条件。``reviewStatus`` 为空表示不按状态筛选。 */
export interface EvidenceScope {
  docId: string;
  category: string;
  collectionId: string;
  reviewStatus: string;
}

export function emptyScope(): EvidenceScope {
  return { docId: '', category: '', collectionId: '', reviewStatus: '' };
}

/** 范围键：只描述「这一批结果是什么」，页码不参与。 */
export function scopeKey(scope: EvidenceScope): string {
  return [scope.docId, scope.category, scope.collectionId, scope.reviewStatus]
    .map((value) => value.trim())
    .join('|');
}

/** 人类可读的筛选说明，用于「选择已清空」这类提示。 */
export function describeScope(scope: EvidenceScope): string {
  const parts: string[] = [];
  if (scope.docId) parts.push('来源文献');
  if (scope.category) parts.push(`分类 ${scope.category}`);
  if (scope.collectionId) parts.push('集合');
  if (scope.reviewStatus) parts.push('核验状态');
  return parts.length ? parts.join('、') : '全部摘录';
}

export interface SelectionState {
  /** 按勾选先后保存的摘录 ID，跨页保留 */
  ids: string[];
  /** 这些选择对应的范围键 */
  scope: string;
}

export function emptySelection(): SelectionState {
  return { ids: [], scope: '' };
}

export function createSelection(scope: string): SelectionState {
  return { ids: [], scope };
}

export function isSelected(state: SelectionState, id: string): boolean {
  return state.ids.includes(id);
}

export function toggleOne(state: SelectionState, id: string): SelectionState {
  if (state.ids.includes(id)) {
    return { ...state, ids: state.ids.filter((value) => value !== id) };
  }
  return { ...state, ids: [...state.ids, id] };
}

/** 本页全选/取消：只影响当前页的条目，其余页的勾选原样保留。 */
export function setPageSelection(state: SelectionState, pageIds: string[],
                                 checked: boolean): SelectionState {
  const others = state.ids.filter((id) => !pageIds.includes(id));
  return { ...state, ids: checked ? [...others, ...pageIds] : others };
}

export function clearSelection(): SelectionState {
  return { ids: [], scope: '' };
}

export function countSelected(state: SelectionState): number {
  return state.ids.length;
}

/** 当前页里有多少条被选中（用于「本页已选 3 / 共 30」）。 */
export function selectedOnPage(state: SelectionState, pageIds: string[]): number {
  return pageIds.filter((id) => state.ids.includes(id)).length;
}

export interface ReconcileResult {
  next: SelectionState;
  /** 需要向用户解释时才非空 */
  notice: string | null;
}

/**
 * 筛选变化后对齐选择。
 *
 * 范围没变（只是翻页或普通刷新）→ 原样保留。
 * 范围变了 → 清空并说明原因；绝不把旧选择带进新范围。
 */
export function reconcileSelection(state: SelectionState, nextScope: string): ReconcileResult {
  if (state.ids.length === 0) {
    return { next: { ids: [], scope: nextScope }, notice: null };
  }
  if (state.scope === nextScope) {
    return { next: state, notice: null };
  }
  return {
    next: { ids: [], scope: nextScope },
    notice: `筛选条件已改变（现在是「${describeScopeFromKey(nextScope)}」），已清空之前选择的 ${state.ids.length} 条，避免把它们带进新范围。`,
  };
}

/** 由范围键还原可读说明（键的顺序见 ``scopeKey``）。 */
export function describeScopeFromKey(key: string): string {
  const [docId, category, collectionId, reviewStatus] = key.split('|');
  return describeScope({ docId: docId ?? '', category: category ?? '',
                        collectionId: collectionId ?? '', reviewStatus: reviewStatus ?? '' });
}

/**
 * 列表正在按新条件加载时（``stale``），旧行不能用于批量操作：
 * 勾选与批量按钮一律禁用，返回 null 让界面自己决定怎么提示。
 */
export function bulkAvailability(selection: SelectionState, stale: boolean,
                                 loading: boolean): { enabled: boolean; reason: string | null } {
  if (stale) {
    return { enabled: false, reason: '正在按新的筛选条件重新取数，请等列表加载完成后再批量操作。' };
  }
  if (loading && selection.ids.length === 0) {
    return { enabled: false, reason: '正在读取摘录列表。' };
  }
  if (selection.ids.length === 0) {
    return { enabled: false, reason: '还没有勾选任何摘录。' };
  }
  return { enabled: true, reason: null };
}