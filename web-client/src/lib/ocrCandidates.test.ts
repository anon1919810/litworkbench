/**
 * OCR 候选对照的纯逻辑：通用插入/删除/替换差异，以及文件页范围解析。
 *
 * 这里锁定 Task C 的两条硬约束：
 * 1. 差异必须是通用的字符级插入/删除/替换（不是硬编码某个字），按 Unicode 码点处理，
 *    大文本有界退回时保留完整正文，绝不截断待采用的真实文字。
 * 2. 文件页范围由全部 unit 摘要解析：只认 file_page（不把印刷页当文件页）、去重、
 *    上限 1000；Word / 超出已知页 / 空范围都在前端解释。
 */
import { describe, expect, it } from 'vitest';
import { diffText, resolveUnitRange } from './ocrCandidates';
import type { UnitPageSummary } from './ocrCandidates';

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

describe('diffText', () => {
  it('替换：相邻的删除与插入合并为 replace，两侧文字分别保留', () => {
    const result = diffText('媒妁之言', '媒约之言');
    expect(result.bounded).toBe(false);
    const replace = result.spans.find((span) => span.kind === 'replace');
    expect(replace).toBeDefined();
    expect(replace?.current).toBe('妁');
    expect(replace?.candidate).toBe('约');
    const equal = result.spans.find((span) => span.kind === 'equal');
    expect(equal?.current).toBe('媒');
    expect(result.changed).toBeGreaterThan(0);
  });

  it('插入：候选多出的字被单独标出', () => {
    const result = diffText('甲乙丙', '甲乙丁丙');
    const insert = result.spans.find((span) => span.kind === 'insert');
    expect(insert?.candidate).toBe('丁');
    expect(insert?.current).toBe('');
    expect(result.spans.some((span) => span.kind === 'delete')).toBe(false);
  });

  it('删除：现用多出的字被单独标出', () => {
    const result = diffText('甲乙丙丁', '甲乙丁');
    const remove = result.spans.find((span) => span.kind === 'delete');
    expect(remove?.current).toBe('丙');
    expect(remove?.candidate).toBe('');
  });

  it('Unicode 扩展字符按码点处理，不拆代理对', () => {
    const result = diffText('𠀀甲', '𠀀乙');
    const equal = result.spans.find((span) => span.kind === 'equal');
    const replace = result.spans.find((span) => span.kind === 'replace');
    expect(equal?.current).toBe('𠀀');
    expect(replace?.current).toBe('甲');
    expect(replace?.candidate).toBe('乙');
    for (const span of result.spans) {
      expect(LONE_SURROGATE.test(span.current)).toBe(false);
      expect(LONE_SURROGATE.test(span.candidate)).toBe(false);
    }
  });

  it('超过预算时退回并排全文，完整正文不被截断', () => {
    const base = '甲'.repeat(400);
    const candidate = '乙'.repeat(400);
    const result = diffText(base, candidate, 20);
    expect(result.bounded).toBe(true);
    expect(result.base).toBe(base);
    expect(result.candidate).toBe(candidate);
    expect(result.spans).toEqual([]);
  });

  it('相同文字不产生差异', () => {
    const result = diffText('父母之命', '父母之命');
    expect(result.spans.every((span) => span.kind === 'equal')).toBe(true);
    expect(result.changed).toBe(0);
  });
});

const PAGE_UNITS: UnitPageSummary[] = [
  { id: 'u10', file_page: 10, kind: 'page' },
  { id: 'u11', file_page: 11, kind: 'page' },
  { id: 'u12', file_page: 12, kind: 'page' },
  { id: 'u12', file_page: 12, kind: 'page' }, // 合并分页时可能出现的重复项
  { id: 'u13', file_page: 13, kind: 'page' },
];

describe('resolveUnitRange', () => {
  it('当前页模式只取当前单元', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'page', { currentUnitId: 'u12' });
    expect(result.error).toBeNull();
    expect(result.unitIds).toEqual(['u12']);
    expect(result.count).toBe(1);
  });

  it('文件页范围只认 file_page，并去重', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'range', { from: 11, to: 12 });
    expect(result.error).toBeNull();
    expect(result.unitIds).toEqual(['u11', 'u12']);
    expect(result.count).toBe(2);
  });

  it('超出已知页范围给出可读解释且不选页', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'range', { from: 90, to: 95 });
    expect(result.unitIds).toEqual([]);
    expect(result.error).toContain('超出');
  });

  it('非法范围（起点大于终点）给出解释', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'range', { from: 12, to: 5 });
    expect(result.error).not.toBeNull();
    expect(result.unitIds).toEqual([]);
  });

  it('Word 单元没有文件页时拒绝页面范围', () => {
    const result = resolveUnitRange([{ id: 'w1', file_page: null, kind: 'docx' }], 'range', { from: 1, to: 3 });
    expect(result.unitIds).toEqual([]);
    expect(result.error).toContain('Word');
  });

  it('当前页是 Word（没有文件页）时拒绝单页候选', () => {
    const result = resolveUnitRange([{ id: 'w1', file_page: null, kind: 'docx' }], 'page', { currentUnitId: 'w1' });
    expect(result.unitIds).toEqual([]);
    expect(result.error).toContain('Word');
  });

  it('终点超出已知文件页时给出解释且不选页（不静默截断）', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'range', { from: 12, to: 99 });
    expect(result.unitIds).toEqual([]);
    expect(result.error).toContain('超出');
  });

  it('整本文献去重后超过 1000 页时拒绝', () => {
    const many: UnitPageSummary[] = Array.from({ length: 1001 }, (_, index) => ({
      id: `u${index + 1}`,
      file_page: index + 1,
      kind: 'page',
    }));
    const result = resolveUnitRange(many, 'document');
    expect(result.count).toBe(1001);
    expect(result.error).toContain('1000');
  });

  it('整本文献在 1000 页以内时全部纳入', () => {
    const result = resolveUnitRange(PAGE_UNITS, 'document');
    expect(result.error).toBeNull();
    expect(result.unitIds).toEqual(['u10', 'u11', 'u12', 'u13']);
    expect(result.count).toBe(4);
  });
});
