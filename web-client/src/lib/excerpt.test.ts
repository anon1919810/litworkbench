import { describe, expect, it } from 'vitest';
import { draftFromSelection, quoteContext, relinkDraft, resolveQuoteDraft } from './excerpt';
import { ocrOverlayBoxes } from './ocrOverlay';
import { codepointOffsetToUtf16, utf16OffsetToCodepoint } from './unicode';
import type { OcrInfo } from '../api/types';

describe('摘录草稿校验', () => {
  // 文0庙1在2东3关4，5文6庙7学8宫9与10文11庙12同13址14。15文16庙17位…
  const text = '文庙在东关，文庙学宫与文庙同址。文庙位于本县东北角。';

  it('偏移处的切片与引文一致才算通过', () => {
    expect(resolveQuoteDraft(text, { quote: '文庙', startOffset: 0 }).valid).toBe(true);
    expect(resolveQuoteDraft(text, { quote: '文庙', startOffset: 6 }).valid).toBe(true);
    expect(resolveQuoteDraft(text, { quote: '文庙', startOffset: 1 }).valid).toBe(false);
  });

  it('错位时给出可执行的中文提示，并列出全部出现位置', () => {
    const result = resolveQuoteDraft(text, { quote: '文庙', startOffset: 1 });
    expect(result.valid).toBe(false);
    expect(result.error).toContain('出现');
    expect(result.occurrences).toEqual([0, 6, 11, 16]);
  });

  it('空引文被拒绝', () => {
    const result = resolveQuoteDraft(text, { quote: '   ', startOffset: 0 });
    expect(result.valid).toBe(false);
    expect(result.error).toContain('不能为空');
  });

  it('重新编辑引文后仍成立的那次出现会被保留', () => {
    expect(relinkDraft(text, { quote: '文庙', startOffset: 11 })).toEqual({ quote: '文庙', startOffset: 11 });
    // 偏移不再成立时回到第一次出现，交由用户确认
    expect(relinkDraft(text, { quote: '文庙学宫', startOffset: 11 })).toEqual({ quote: '文庙学宫', startOffset: 6 });
    // 原文没有这段文字时保持原样，交给校验报错
    expect(relinkDraft(text, { quote: '不存在', startOffset: 3 })).toEqual({ quote: '不存在', startOffset: 3 });
  });

  it('选区草稿的码点偏移与 UTF-16 选区对应', () => {
    const withEmoji = '📜文庙在东关';
    const draft = draftFromSelection(withEmoji, { start: 2, end: 4, text: '文庙' });
    expect(draft).toEqual({ quote: '文庙', startOffset: 1 });
    expect(resolveQuoteDraft(withEmoji, draft!).valid).toBe(true);
    expect(utf16OffsetToCodepoint(withEmoji, codepointOffsetToUtf16(withEmoji, 1))).toBe(1);
    expect(draftFromSelection(withEmoji, { start: 0, end: 0, text: '' })).toBeNull();
  });

  it('上下文预览取自原文切片', () => {
    const context = quoteContext(text, { quote: '文庙学宫', startOffset: 6 }, 2);
    expect(context.before).toBe('关，');
    expect(context.after).toBe('与文');
  });
});

describe('OCR 行框只画真实坐标', () => {
  const box = (...points: Array<[number, number]>): Array<[number, number]> => points;
  const ocr: OcrInfo = {
    engine: 'rapidocr',
    mean_confidence: 0.91,
    render_width: 1000,
    render_height: 1500,
    lines: [
      { box: box([10, 20], [110, 22], [109, 60], [9, 58]), text: '第一行', confidence: 0.93 },
      { box: box([10, 80], [210, 80], [210, 120], [10, 120]), text: '第二行', confidence: 0.88 },
    ],
  };

  it('box 多边形换算成百分比外接矩形', () => {
    const boxes = ocrOverlayBoxes(ocr);
    expect(boxes).toHaveLength(2);
    // 第一个多边形的最小 x 是 9（最后一个顶点），最大 x 是 110
    expect(boxes![0].left).toBeCloseTo(0.9, 4);
    expect(boxes![0].top).toBeCloseTo((20 / 1500) * 100, 4);
    expect(boxes![0].width).toBeCloseTo(10.1, 4);
    expect(boxes![0].text).toBe('第一行');
  });

  it('缺少渲染尺寸、坐标不全或坐标非法时不画任何框', () => {
    expect(ocrOverlayBoxes({ lines: ocr.lines })).toBeNull();
    expect(ocrOverlayBoxes({ ...ocr, render_width: 0 })).toBeNull();
    expect(ocrOverlayBoxes({ ...ocr, lines: [{ box: [] }, ...(ocr.lines ?? [])] })).toBeNull();
    expect(ocrOverlayBoxes({ ...ocr, lines: [{ box: [[0, 0], [10, 0]] }] })).toBeNull();
    expect(ocrOverlayBoxes({ ...ocr, lines: [{ box: [[-5, 0], [10, 0], [10, 10]] }] })).toBeNull();
    expect(ocrOverlayBoxes(null)).toBeNull();
  });

  it('完全落在页面之外的行视为不可靠', () => {
    const outside: OcrInfo = { ...ocr, lines: [{ box: box([1000, 20], [1200, 22], [1199, 60], [1000, 58]) }] };
    expect(ocrOverlayBoxes(outside)).toBeNull();
  });

  it('越出右边界一部分的行按真实坐标裁剪，不外推', () => {
    const partial: OcrInfo = { ...ocr, lines: [{ box: box([900, 20], [1200, 22], [1199, 60], [900, 58]) }] };
    const boxes = ocrOverlayBoxes(partial);
    expect(boxes![0].width).toBeCloseTo(10, 4);
  });

  it('文字已被校订时不做行框映射', () => {
    expect(ocrOverlayBoxes(ocr, { textVersion: 'corrected' })).toBeNull();
    expect(ocrOverlayBoxes(ocr, { textVersion: 'original' })).toHaveLength(2);
  });

  it('兼容个别实现给出的 bbox', () => {
    const boxes = ocrOverlayBoxes({ ...ocr, lines: [{ bbox: [100, 300, 200, 50] }] });
    expect(boxes![0].left).toBeCloseTo(10);
    expect(boxes![0].top).toBeCloseTo(20);
    expect(boxes![0].width).toBeCloseTo(20);
  });
});