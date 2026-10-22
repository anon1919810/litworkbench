import { describe, expect, it } from 'vitest';
import { filePageLabel, printedPageLabel, versionLabel, jobUnitLabel, jobUnitOpenable, jobUnitStateInfo } from './format';
import { summarizeCoverage, normalizeCoveragePayload } from './coverage';
import { segmentByTerms, queryTerms } from './highlight';

describe('文件页位置', () => {
  it('总页数未知时只说“文件第 N 页”，不谎称统计中', () => {
    expect(filePageLabel(7, null)).toBe('文件第 7 页');
    expect(filePageLabel(7, null)).not.toContain('统计中');
  });

  it('总页数已知时给出真实总数', () => {
    expect(filePageLabel(7, 12)).toBe('文件第 7 页 / 共 12 页');
  });

  it('Word 文档单元不套用页码说法', () => {
    expect(filePageLabel(null, 12)).toBe('文件页不适用（文档单元）');
  });
});

describe('任务逐页检查点的真实状态词', () => {
  it('映射服务端实际使用的 work / done / pending / failed', () => {
    expect(jobUnitStateInfo('work')).toEqual({ label: '未处理', tone: 'neutral' });
    expect(jobUnitStateInfo('done')).toEqual({ label: '已完成', tone: 'info' });
    expect(jobUnitStateInfo('pending')).toEqual({ label: '无文字待确认', tone: 'warn' });
    expect(jobUnitStateInfo('failed')).toEqual({ label: '失败', tone: 'error' });
  });

  it('未知状态原样显示，不臆造中文', () => {
    expect(jobUnitStateInfo('mystery').label).toBe('mystery');
    expect(jobUnitStateInfo(null).label).toBe('—');
  });

  it('只有未处理的项不能打开阅读区（单元可能还没生成）', () => {
    expect(jobUnitOpenable('work')).toBe(false);
    expect(jobUnitOpenable('done')).toBe(true);
    expect(jobUnitOpenable('pending')).toBe(true);
    expect(jobUnitOpenable('failed')).toBe(true);
  });

  it('position 是 0 起的任务内序号，界面显示第 N 项', () => {
    expect(jobUnitLabel('pdf', 0)).toBe('文件第 1 页');
    expect(jobUnitLabel('pdf', 6)).toBe('文件第 7 页');
    expect(jobUnitLabel('pdf', 90)).toBe('文件第 91 页');
    // Word 的段落/表格单元不冒充书的物理页码
    expect(jobUnitLabel('docx', 0)).toBe('文档单元 1');
    expect(jobUnitLabel('docx', 11)).toBe('文档单元 12');
    expect(jobUnitLabel(null, 2)).toBe('文档单元 3');
    expect(jobUnitLabel('pdf', null)).toBe('位置未知');
  });
});

describe('印刷页码按字符串显示', () => {
  it('阿拉伯数字、罗马数字与中文标注都按原样显示', () => {
    expect(printedPageLabel('775', 'confirmed')).toBe('印刷页码 775（已确认）');
    expect(printedPageLabel('MCLXXV', 'candidate')).toBe('印刷页码 MCLXXV（候选）');
    expect(printedPageLabel('七七五', 'none')).toBe('印刷页码 七七五（未确认）');
  });

  it('空字符串与 null 都表示未填写', () => {
    expect(printedPageLabel('', 'none')).toBe('印刷页码未填');
    expect(printedPageLabel(null, 'candidate')).toBe('印刷页码未填（候选）');
  });
});

describe('文本版本', () => {
  it('original / corrected 显示为识别原文与校订版', () => {
    expect(versionLabel('original')).toBe('识别原文');
    expect(versionLabel('corrected')).toBe('校订版');
    expect(versionLabel(null)).toBe('未知版本');
  });
});

describe('覆盖口径不制造假数字', () => {
  it('页数未知时只说统计中，不给百分比', () => {
    const summary = summarizeCoverage(
      normalizeCoveragePayload({
        known_pdf_pages: null,
        covered_pdf_pages: null,
        pending_pdf_pages: null,
        failed_pdf_pages: null,
        unknown_documents: 1,
        indexed_docx: 2,
        total_docx: 2,
      }),
    );
    expect(summary.ratio).toBeNull();
    expect(summary.text).toContain('统计中');
    expect(summary.text).toContain('Word 已索引 2/2 份');
  });

  it('已知页数时给出真实比例与待处理数量', () => {
    const summary = summarizeCoverage(
      normalizeCoveragePayload({
        known_pdf_pages: 91,
        covered_pdf_pages: 38,
        pending_pdf_pages: 50,
        failed_pdf_pages: 3,
        unknown_documents: 0,
        indexed_docx: 0,
        total_docx: 2,
      }),
    );
    expect(summary.ratio).toEqual({ covered: 38, known: 91 });
    expect(summary.hasBacklog).toBe(true);
    expect(summary.text).toContain('PDF 已覆盖 38/91 页');
  });

  it('缺字段的响应按未知处理，不补 0', () => {
    const coverage = normalizeCoveragePayload({ indexed_docx: 2, total_docx: 2 });
    expect(coverage?.known_pdf_pages).toBeNull();
    expect(coverage?.covered_pdf_pages).toBeNull();
  });
});

describe('片段标词只切普通文本', () => {
  it('短语模式整串标注，其它模式按词标注', () => {
    expect(queryTerms('文庙 学宫', 'phrase')).toEqual(['文庙 学宫']);
    expect(queryTerms('文庙 学宫', 'all_terms')).toEqual(['文庙', '学宫']);
    const segments = segmentByTerms('文庙在东关，文庙学宫', ['文庙']);
    expect(segments.filter((item) => item.hit)).toHaveLength(2);
    expect(segments.map((item) => item.text).join('')).toBe('文庙在东关，文庙学宫');
  });

  it('没有检索词时整段原样返回', () => {
    expect(segmentByTerms('任意文字', [])).toEqual([{ text: '任意文字', hit: false }]);
  });
});