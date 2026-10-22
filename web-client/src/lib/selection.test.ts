import { describe, expect, it } from 'vitest';
import { computeRangeOffsets } from './selection';
import { draftFromSelection } from './excerpt';
import { codepointOffsetToUtf16, sliceMatchesQuote, utf16OffsetToCodepoint } from './unicode';

/** 构造阅读区的纯文本元素：一个 <p> 里一个文本节点。 */
function singleTextNode(text: string): HTMLParagraphElement {
  const root = document.createElement('p');
  root.textContent = text;
  return root;
}

describe('单文本节点上的选区', () => {
  const text = '位于安康新城东北角，元代建筑，明清时多次维修扩建。';
  const root = singleTextNode(text);
  const node = root.firstChild as Text;

  it('文本节点端点给出精确 UTF-16 偏移', () => {
    const slice = computeRangeOffsets(root, { startContainer: node, startOffset: 2, endContainer: node, endOffset: 6 });
    expect(slice).toEqual({ start: 2, end: 6, text: '安康新城' });
  });

  it('重复文字的第二处选区不会退回第一处', () => {
    const repeated = '文庙在东关，文庙学宫与文庙同址。';
    const root = singleTextNode(repeated);
    const node = root.firstChild as Text;
    const secondStart = repeated.indexOf('文庙', 1);
    expect(secondStart).toBe(6);
    const slice = computeRangeOffsets(root, {
      startContainer: node,
      startOffset: secondStart,
      endContainer: node,
      endOffset: secondStart + 2,
    });
    expect(slice?.start).toBe(6);
    expect(slice?.text).toBe('文庙');
    expect(utf16OffsetToCodepoint(repeated, slice!.start)).toBe(6);
    expect(sliceMatchesQuote(repeated, utf16OffsetToCodepoint(repeated, slice!.start), slice!.text)).toBe(true);
  });

  it('含代理对的选区换算成码点偏移后仍能切回原文', () => {
    const withEmoji = '汉😀文庙在东关';
    const root2 = singleTextNode(withEmoji);
    const node2 = root2.firstChild as Text;
    const start = withEmoji.indexOf('文庙');
    const slice = computeRangeOffsets(root2, { startContainer: node2, startOffset: start, endContainer: node2, endOffset: start + 2 });
    expect(slice).not.toBeNull();
    const draft = draftFromSelection(withEmoji, slice!);
    expect(draft).toEqual({ quote: '文庙', startOffset: 2 });
    expect(utf16OffsetToCodepoint(withEmoji, start)).toBe(2);
    expect(codepointOffsetToUtf16(withEmoji, draft!.startOffset)).toBe(start);
  });
});

describe('元素端点（含末尾边界）', () => {
  it('root(0) → root(1) 在单一子节点上等于整段文本', () => {
    const root = singleTextNode('文庙学宫');
    const slice = computeRangeOffsets(root, { startContainer: root, startOffset: 0, endContainer: root, endOffset: 1 });
    expect(slice).toEqual({ start: 0, end: 4, text: '文庙学宫' });
  });

  it('元素末尾边界 offset === childNodes.length 落在子节点末尾', () => {
    const root = document.createElement('div');
    const first = document.createElement('span');
    first.textContent = '文庙';
    const second = document.createElement('span');
    second.textContent = '学宫';
    root.append(first, second);
    const slice = computeRangeOffsets(root, { startContainer: root, startOffset: 1, endContainer: root, endOffset: 2 });
    expect(slice).toEqual({ start: 2, end: 4, text: '学宫' });
  });

  it('嵌套结构里跨文本节点的偏移按累计长度计算', () => {
    const root = document.createElement('div');
    const a = document.createElement('p');
    a.textContent = '第一段';
    const b = document.createElement('p');
    b.textContent = '第二段文庙';
    root.append(a, b);
    const textNode = b.firstChild as Text;
    const slice = computeRangeOffsets(root, {
      startContainer: a.firstChild as Text,
      startOffset: 2,
      endContainer: textNode,
      endOffset: textNode.data.length,
    });
    expect(slice?.start).toBe(2);
    expect(slice?.text).toBe('段第二段文庙');
  });

  it('端点落在 root 之外或范围倒置时返回 null', () => {
    const root = singleTextNode('文庙');
    const outside = singleTextNode('其它');
    expect(
      computeRangeOffsets(root, { startContainer: outside.firstChild as Text, startOffset: 0, endContainer: outside.firstChild as Text, endOffset: 1 }),
    ).toBeNull();
    const node = root.firstChild as Text;
    expect(computeRangeOffsets(root, { startContainer: node, startOffset: 2, endContainer: node, endOffset: 1 })).toBeNull();
    expect(computeRangeOffsets(root, { startContainer: node, startOffset: 1, endContainer: node, endOffset: 1 })).toBeNull();
    expect(computeRangeOffsets(root, { startContainer: root, startOffset: 0, endContainer: root, endOffset: 9 })).toBeNull();
  });
});