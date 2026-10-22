/**
 * 覆盖对象的诚实表述：页数未知就写“统计中”，不生成假百分比。
 */
import type { Coverage } from '../api/types';

export interface CoverageSummary {
  /** 一行覆盖说明，可直接显示 */
  text: string;
  /** 已知 PDF 页数时的 covered/known，否则 null */
  ratio: { covered: number; known: number } | null;
  /** 是否有待处理或失败页 */
  hasBacklog: boolean;
}

/**
 * 契约写作「GET /coverage 返回覆盖对象」，实现可能直接返回对象或包一层 {coverage}。
 * 两种形状都接受，字段缺失按 null 处理，绝不用 0 顶替未知页数。
 */
export function normalizeCoveragePayload(payload: unknown): Coverage | null {
  if (!payload || typeof payload !== 'object') return null;
  const outer = payload as { coverage?: unknown };
  const source = (
    outer.coverage && typeof outer.coverage === 'object' ? outer.coverage : payload
  ) as Record<string, unknown>;

  const num = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);
  const keys: Array<keyof Coverage> = [
    'known_pdf_pages',
    'covered_pdf_pages',
    'pending_pdf_pages',
    'failed_pdf_pages',
    'unknown_documents',
    'indexed_docx',
    'total_docx',
  ];
  const result = {} as Coverage;
  for (const key of keys) result[key] = num(source[key]);
  return result;
}

export function summarizeCoverage(coverage: Coverage | null | undefined): CoverageSummary {
  if (!coverage) {
    return { text: '覆盖情况读取中。', ratio: null, hasBacklog: false };
  }
  const known = coverage.known_pdf_pages;
  const covered = coverage.covered_pdf_pages;
  const pending = coverage.pending_pdf_pages;
  const failed = coverage.failed_pdf_pages;
  const parts: string[] = [];

  if (known === null || known === undefined) {
    parts.push('PDF 页数统计中');
  } else if (covered === null || covered === undefined) {
    parts.push(`PDF 共 ${known} 页，覆盖统计中`);
  } else {
    parts.push(`PDF 已覆盖 ${covered}/${known} 页`);
  }
  if (pending) parts.push(`${pending} 页待处理`);
  if (failed) parts.push(`${failed} 页失败`);
  if (coverage.unknown_documents) parts.push(`${coverage.unknown_documents} 份页数未知`);

  const totalDocx = coverage.total_docx ?? 0;
  const indexedDocx = coverage.indexed_docx ?? 0;
  parts.push(`Word 已索引 ${indexedDocx}/${totalDocx} 份`);

  return {
    text: parts.join('，'),
    ratio: known === null || known === undefined || covered === null || covered === undefined ? null : { covered, known },
    hasBacklog: Boolean(pending || failed),
  };
}