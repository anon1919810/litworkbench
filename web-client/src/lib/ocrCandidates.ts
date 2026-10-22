/**
 * OCR 候选对照的纯逻辑。
 *
 * 1. `diffText`：通用字符级插入/删除/替换差异，按 Unicode 码点处理；大文本有界退回，
 *    退回时保留完整正文，绝不截断待采用的真实文字。为避免巨大 LCS 矩阵，行列乘积超过
 *    上限时只把中间段标为整体替换。
 * 2. `resolveUnitRange`：把全部 unit 摘要解析成显式 unit_ids。只认 file_page（不把印刷页
 *    当文件页）、去重、上限 1000；Word / 超出已知页 / 空范围都在前端解释。
 */

export type RangeMode = 'page' | 'range' | 'document';

export interface UnitPageSummary {
  id: string;
  file_page: number | null;
  kind?: string;
}

export interface RangeResolution {
  unitIds: string[];
  /** 去重后实际会处理的页数（即使因超限被拒绝也给出真实计数） */
  count: number;
  error: string | null;
  knownMaxPage: number | null;
}

export const MAX_CANDIDATE_PAGES = 1000;

function dedupe(ids: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const id of ids) {
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

function isPositiveInt(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1;
}

/**
 * 解析候选生成范围。`page` 只取当前单元；`range` 按 file_page 过滤；
 * `document` 取整本。任何错误都返回 unitIds=[]，调用方不得据错误结果提交任务。
 */
export function resolveUnitRange(
  units: UnitPageSummary[],
  mode: RangeMode,
  options: { currentUnitId?: string; from?: number | null; to?: number | null } = {},
): RangeResolution {
  const knownMaxPage = units.reduce<number | null>((max, unit) => {
    if (unit.file_page === null || unit.file_page === undefined) return max;
    return max === null || unit.file_page > max ? unit.file_page : max;
  }, null);

  if (mode === 'page') {
    if (!options.currentUnitId) {
      return { unitIds: [], count: 0, error: '缺少当前单元，无法生成候选。', knownMaxPage };
    }
    const current = units.find((unit) => unit.id === options.currentUnitId);
    if (current && (current.file_page === null || current.file_page === undefined)) {
      return {
        unitIds: [],
        count: 0,
        error: 'Word 文献没有文件页，候选只支持按文件页的 PDF 页面。',
        knownMaxPage,
      };
    }
    return { unitIds: [options.currentUnitId], count: 1, error: null, knownMaxPage };
  }

  const hasWordUnit = units.some((unit) => unit.file_page === null || unit.file_page === undefined);
  if (hasWordUnit) {
    return {
      unitIds: [],
      count: 0,
      error: '本文献包含没有文件页的 Word 单元，不能按文件页生成候选。',
      knownMaxPage,
    };
  }

  if (!units.length) {
    return { unitIds: [], count: 0, error: '本文献没有可处理的页面。', knownMaxPage };
  }

  if (mode === 'document') {
    const ids = dedupe(units.map((unit) => unit.id));
    if (ids.length > MAX_CANDIDATE_PAGES) {
      return {
        unitIds: [],
        count: ids.length,
        error: `一次最多生成 ${MAX_CANDIDATE_PAGES} 页候选。`,
        knownMaxPage,
      };
    }
    return { unitIds: ids, count: ids.length, error: null, knownMaxPage };
  }

  const from = options.from;
  const to = options.to;
  if (!isPositiveInt(from) || !isPositiveInt(to) || from > to) {
    return {
      unitIds: [],
      count: 0,
      error: '请输入有效的文件页范围：起止为不小于 1 的整数，且起始不大于结束。',
      knownMaxPage,
    };
  }
  if (knownMaxPage === null) {
    return { unitIds: [], count: 0, error: '无法确定本文献的文件页范围。', knownMaxPage };
  }
  if (from > knownMaxPage) {
    return {
      unitIds: [],
      count: 0,
      error: `超出已知文件页范围（本文件共 ${knownMaxPage} 页）。`,
      knownMaxPage,
    };
  }
  if (to > knownMaxPage) {
    return {
      unitIds: [],
      count: 0,
      error: `结束页超出已知文件页范围（本文件共 ${knownMaxPage} 页）。`,
      knownMaxPage,
    };
  }

  const selected = dedupe(
    units.filter((unit) => unit.file_page !== null && unit.file_page >= from && unit.file_page <= to).map((unit) => unit.id),
  );
  if (!selected.length) {
    return { unitIds: [], count: 0, error: '该文件页范围内没有可处理的页面。', knownMaxPage };
  }
  if (selected.length > MAX_CANDIDATE_PAGES) {
    return {
      unitIds: [],
      count: selected.length,
      error: `一次最多生成 ${MAX_CANDIDATE_PAGES} 页候选。`,
      knownMaxPage,
    };
  }
  return { unitIds: selected, count: selected.length, error: null, knownMaxPage };
}

export type DiffKind = 'equal' | 'insert' | 'delete' | 'replace';

export interface DiffSpan {
  kind: DiffKind;
  /** 基准（现用/旧 OCR）一侧的文字；插入段为空串 */
  current: string;
  /** 候选一侧的文字；删除段为空串 */
  candidate: string;
}

export interface DiffResult {
  spans: DiffSpan[];
  /** 变化的码点数（删除 + 插入） */
  changed: number;
  /** 是否因超过预算退回并排全文对照 */
  bounded: boolean;
  /** 完整基准正文，始终未截断 */
  base: string;
  /** 完整候选正文，始终未截断 */
  candidate: string;
}

const DEFAULT_DIFF_BUDGET = 4000;
/** LCS 矩阵单元格上限，避免大文本冻结网页。 */
const DIFF_MATRIX_CAP = 40_000;

function countCodepoints(value: string): number {
  return Array.from(value).length;
}

interface DiffOp {
  type: 'equal' | 'delete' | 'insert';
  char: string;
}

function lcsOps(a: string[], b: string[]): DiffOp[] {
  const n = a.length;
  const m = b.length;
  const table: Uint16Array[] = [];
  for (let i = 0; i <= n; i += 1) table.push(new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ type: 'equal', char: a[i] });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      ops.push({ type: 'delete', char: a[i] });
      i += 1;
    } else {
      ops.push({ type: 'insert', char: b[j] });
      j += 1;
    }
  }
  while (i < n) {
    ops.push({ type: 'delete', char: a[i] });
    i += 1;
  }
  while (j < m) {
    ops.push({ type: 'insert', char: b[j] });
    j += 1;
  }
  return ops;
}

function groupOps(prefix: string, ops: DiffOp[], suffix: string): DiffSpan[] {
  const spans: DiffSpan[] = [];
  if (prefix) spans.push({ kind: 'equal', current: prefix, candidate: prefix });
  let index = 0;
  while (index < ops.length) {
    if (ops[index].type === 'equal') {
      let text = '';
      while (index < ops.length && ops[index].type === 'equal') {
        text += ops[index].char;
        index += 1;
      }
      spans.push({ kind: 'equal', current: text, candidate: text });
      continue;
    }
    let deleted = '';
    let inserted = '';
    while (index < ops.length && ops[index].type !== 'equal') {
      if (ops[index].type === 'delete') deleted += ops[index].char;
      else inserted += ops[index].char;
      index += 1;
    }
    if (deleted && inserted) spans.push({ kind: 'replace', current: deleted, candidate: inserted });
    else if (deleted) spans.push({ kind: 'delete', current: deleted, candidate: '' });
    else if (inserted) spans.push({ kind: 'insert', current: '', candidate: inserted });
  }
  if (suffix) spans.push({ kind: 'equal', current: suffix, candidate: suffix });
  return spans;
}

/** 通用差异。超过预算时 `bounded=true`、`spans=[]`，但 `base`/`candidate` 始终是完整正文。 */
export function diffText(base: string, candidate: string, budget = DEFAULT_DIFF_BUDGET): DiffResult {
  const a = Array.from(base);
  const b = Array.from(candidate);
  if (a.length > budget || b.length > budget) {
    return { spans: [], changed: a.length + b.length, bounded: true, base, candidate };
  }

  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }

  const prefix = a.slice(0, start).join('');
  const suffix = a.slice(endA).join('');
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);

  let spans: DiffSpan[];
  if (!midA.length && !midB.length) {
    spans = groupOps(prefix, [], suffix);
  } else if (!midA.length) {
    spans = groupOps(prefix, [{ type: 'insert', char: midB.join('') }], suffix);
  } else if (!midB.length) {
    spans = groupOps(prefix, [{ type: 'delete', char: midA.join('') }], suffix);
  } else if (midA.length * midB.length > DIFF_MATRIX_CAP) {
    // 中间段太大：只做整体替换，仍保留两侧完整文字（不截断）。
    spans = [];
    if (prefix) spans.push({ kind: 'equal', current: prefix, candidate: prefix });
    spans.push({ kind: 'replace', current: midA.join(''), candidate: midB.join('') });
    if (suffix) spans.push({ kind: 'equal', current: suffix, candidate: suffix });
  } else {
    spans = groupOps(prefix, lcsOps(midA, midB), suffix);
  }

  const changed = spans.reduce(
    (total, span) =>
      span.kind === 'equal' ? total : total + countCodepoints(span.current) + countCodepoints(span.candidate),
    0,
  );
  return { spans, changed, bounded: false, base, candidate };
}
