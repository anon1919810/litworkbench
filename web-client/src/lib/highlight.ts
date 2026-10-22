/**
 * 把后端给的片段原文按检索词切成普通文本片段，供 React 渲染成文本节点或 <mark>。
 * 全程不使用 innerHTML，也不解析任何后端 HTML。
 */

export interface TextSegment {
  text: string;
  hit: boolean;
}

/** 从查询词中取出用于标注的字面片段（短语模式取整句，其它模式按空白切词）。 */
export function queryTerms(query: string, mode: 'phrase' | 'all_terms' | 'any_terms'): string[] {
  const trimmed = query.trim();
  if (!trimmed) return [];
  if (mode === 'phrase') return [trimmed];
  const parts = trimmed.split(/\s+/u).filter((part) => part.length > 0);
  return parts.length ? parts : [trimmed];
}

/**
 * 切分 text：命中的部分 hit=true，其余 false。
 * 使用 code point 遍历，代理对不会被拆开。
 */
export function segmentByTerms(text: string, terms: string[]): TextSegment[] {
  const needles = terms.map((term) => term.trim()).filter((term) => term.length > 0);
  if (!needles.length || !text) return text ? [{ text, hit: false }] : [];

  // 收集所有非重叠命中区间（优先较长的词，避免短词切断长词）。
  type Span = { start: number; end: number };
  const spans: Span[] = [];
  const ordered = [...needles].sort((a, b) => b.length - a.length);
  for (const needle of ordered) {
    let from = 0;
    for (;;) {
      const at = text.indexOf(needle, from);
      if (at < 0) break;
      const end = at + needle.length;
      if (!spans.some((span) => at < span.end && end > span.start)) spans.push({ start: at, end });
      from = at + 1;
      if (spans.length > 2000) break;
    }
  }
  spans.sort((a, b) => a.start - b.start);

  const segments: TextSegment[] = [];
  let cursor = 0;
  for (const span of spans) {
    if (span.start < cursor) continue;
    if (span.start > cursor) segments.push({ text: text.slice(cursor, span.start), hit: false });
    segments.push({ text: text.slice(span.start, span.end), hit: true });
    cursor = span.end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), hit: false });
  return segments;
}