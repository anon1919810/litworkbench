/**
 * 从 DOM Selection 取得精确的 UTF-16 偏移。
 *
 * 阅读区的识别文字是一个“纯文本元素”（单个文本节点 + pre-wrap 保留换行），
 * 所以选区起止通常直接落在该文本节点上。这里仍按一般情况处理：
 * 端点是元素时，按“该位置之前所有后代文本的长度之和”计算，
 * 因此 element+childNodes.length（末尾）这种边界也必须正确。
 */

/** 与 DOM Range 结构兼容的最小接口，便于纯函数测试。 */
export interface RangeLike {
  startContainer: Node;
  startOffset: number;
  endContainer: Node;
  endOffset: number;
}

export interface SelectedSlice {
  /** UTF-16 偏移（浏览器坐标） */
  start: number;
  end: number;
  /** 选中的原文 */
  text: string;
}

interface TextIndex {
  /** 文本节点 → 它在整体文本中的起始 UTF-16 偏移 */
  base: Map<Text, number>;
  /** 任意子树 → 其全部后代文本的总长度 */
  length: Map<Node, number>;
  total: number;
}

function buildIndex(root: Element): TextIndex {
  const base = new Map<Text, number>();
  const length = new Map<Node, number>();
  let cursor = 0;

  const walk = (node: Node): number => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node as Text;
      base.set(text, cursor);
      const size = text.data.length;
      cursor += size;
      length.set(node, size);
      return size;
    }
    let sum = 0;
    for (let i = 0; i < node.childNodes.length; i += 1) sum += walk(node.childNodes[i]);
    length.set(node, sum);
    return sum;
  };

  // 先给 root 本身占位，再自顶向下累计，保证 Map 里有每个祖先节点的长度。
  length.set(root, 0);
  walk(root);
  return { base, length, total: cursor };
}

/**
 * 计算一个选区端点在 root 内的 UTF-16 偏移。
 * 文本节点：base + min(offset, 长度)；元素：其前 offset 个子节点子树长度之和。
 */
function endpointOffset(index: TextIndex, container: Node, offset: number): number | null {
  if (container.nodeType === Node.TEXT_NODE) {
    const base = index.base.get(container as Text);
    if (base === undefined) return null;
    return base + Math.max(0, Math.min(offset, (container as Text).data.length));
  }
  // 元素端点：offset 允许等于 childNodes.length（表示“最后一个子节点之后”）
  const children = container.childNodes;
  if (!Number.isInteger(offset) || offset < 0 || offset > children.length) return null;
  let sum = 0;
  for (let i = 0; i < offset; i += 1) {
    const size = index.length.get(children[i]);
    if (size === undefined) return null; // 端点不在 root 之内
    sum += size;
  }
  return sum;
}

/**
 * 计算选区相对 root 的 UTF-16 偏移。选区不在 root 内或范围非法时返回 null。
 */
export function computeRangeOffsets(root: Element, range: RangeLike): SelectedSlice | null {
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const index = buildIndex(root);

  const start = endpointOffset(index, range.startContainer, range.startOffset);
  const end = endpointOffset(index, range.endContainer, range.endOffset);
  if (start === null || end === null) return null;
  if (end <= start) return null;

  const text = root.textContent ?? '';
  const from = Math.max(0, Math.min(start, text.length));
  const to = Math.max(0, Math.min(end, text.length));
  return { start: from, end: to, text: text.slice(from, to) };
}

/**
 * 读取当前窗口选区。必须在拥有选区的文档上调用（iframe 场景传入对应 window）。
 */
export function readSelectionSlice(root: Element, win: Window = window): SelectedSlice | null {
  const selection = win.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  return computeRangeOffsets(root, range);
}