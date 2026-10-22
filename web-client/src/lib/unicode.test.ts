import { describe, expect, it } from 'vitest';
import {
  codepointLength,
  codepointOffsetToUtf16,
  findOccurrenceOffsets,
  occurrenceOffset,
  sliceByCodepoints,
  sliceMatchesQuote,
  utf16OffsetToCodepoint,
} from './unicode';

describe('UTF-16 与 Unicode 码点偏移互转', () => {
  it('纯 BMP 文本两者一致', () => {
    const text = '文庙学宫';
    expect(utf16OffsetToCodepoint(text, 0)).toBe(0);
    expect(utf16OffsetToCodepoint(text, 2)).toBe(2);
    expect(codepointOffsetToUtf16(text, 4)).toBe(4);
    expect(codepointLength(text)).toBe(4);
  });

  it('代理对按一个码点计算', () => {
    const text = '汉😀字';
    expect(text.length).toBe(4); // UTF-16 长度：汉 + 代理对 2 + 字
    expect(codepointLength(text)).toBe(3);
    expect(utf16OffsetToCodepoint(text, 3)).toBe(2);
    expect(codepointOffsetToUtf16(text, 2)).toBe(3);
    expect(sliceByCodepoints(text, 1, 2)).toBe('😀');
  });

  it('往返换算一致（含多个代理对）', () => {
    const text = '𠮷𠮷正文文庙𠮷';
    for (let cp = 0; cp <= codepointLength(text); cp += 1) {
      expect(utf16OffsetToCodepoint(text, codepointOffsetToUtf16(text, cp))).toBe(cp);
    }
  });

  it('超出范围的偏移被夹到边界', () => {
    expect(utf16OffsetToCodepoint('文庙', 99)).toBe(2);
    expect(codepointOffsetToUtf16('文庙', 99)).toBe(2);
    expect(sliceByCodepoints('文庙', 1, 0)).toBe('文');
  });
});

describe('同一句重复出现时的偏移', () => {
  // 文0庙1在2东3关4，5文6庙7学8宫9与10文11庙12同13址14。15文16庙17…
  const text = '文庙在东关，文庙学宫与文庙同址。';

  it('给出每一次出现的码点偏移', () => {
    expect(findOccurrenceOffsets(text, '文庙')).toEqual([0, 6, 11]);
  });

  it('可按次序取出指定的那一次', () => {
    expect(occurrenceOffset(text, '文庙', 2)).toBe(6);
    expect(occurrenceOffset(text, '文庙', 3)).toBe(11);
    expect(occurrenceOffset(text, '文庙', 4)).toBeNull();
    expect(occurrenceOffset(text, '文庙', 0)).toBeNull();
  });

  it('切出来的切片与引文逐字一致，且上下文能区分不同出现', () => {
    const start = occurrenceOffset(text, '文庙', 2);
    expect(start).not.toBeNull();
    expect(sliceByCodepoints(text, start as number, (start as number) + 2)).toBe('文庙');
    // 第二处与第三处上下文不同，说明偏移确实区分了具体那一次
    expect(sliceByCodepoints(text, 4, 9)).toBe('关，文庙学');
    expect(sliceByCodepoints(text, 9, 14)).toBe('宫与文庙同');
  });

  it('sliceMatchesQuote 能发现偏移错位', () => {
    expect(sliceMatchesQuote(text, 0, '文庙')).toBe(true);
    expect(sliceMatchesQuote(text, 1, '文庙')).toBe(false);
    expect(sliceMatchesQuote(text, 0, '')).toBe(false);
  });

  it('空引文不产生任何命中', () => {
    expect(findOccurrenceOffsets(text, '')).toEqual([]);
  });
});

describe('混合代理对的重复引文', () => {
  // 😀0甲1😀2甲3😀4
  const text = '😀甲😀甲😀';

  it('偏移按码点计数，可精确切出每次出现', () => {
    const offsets = findOccurrenceOffsets(text, '😀甲');
    expect(offsets).toEqual([0, 2]);
    expect(sliceByCodepoints(text, 2, 4)).toBe('😀甲');
    expect(sliceByCodepoints(text, 4, 5)).toBe('😀');
    expect(occurrenceOffset(text, '😀甲', 2)).toBe(2);
  });
});