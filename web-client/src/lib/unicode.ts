/**
 * Unicode 码点偏移工具。
 *
 * 契约要求：摘录的 start_offset 是 Unicode 码点偏移（与 Python str 索引一致），
 * 而浏览器 DOM Selection 给出的是 UTF-16 偏移（代理对算 2 个单位）。
 * 因此保存摘录前必须换算，且 quote 必须是该偏移处的精确原文切片。
 */

/** 把 UTF-16 偏移换算为 Unicode 码点偏移。落在代理对中间时按整对计 1 个码点。 */
export function utf16OffsetToCodepoint(text: string, utf16Offset: number): number {
  const target = Math.max(0, Math.min(utf16Offset, text.length));
  let count = 0;
  let i = 0;
  while (i < target) {
    const code = text.codePointAt(i) as number;
    i += code > 0xffff ? 2 : 1;
    count += 1;
  }
  return count;
}

/** 把 Unicode 码点偏移换算回 UTF-16 偏移。 */
export function codepointOffsetToUtf16(text: string, codepointOffset: number): number {
  const target = Math.max(0, codepointOffset);
  let count = 0;
  let i = 0;
  while (count < target && i < text.length) {
    const code = text.codePointAt(i) as number;
    i += code > 0xffff ? 2 : 1;
    count += 1;
  }
  return i;
}

/** 文本的 Unicode 码点长度。 */
export function codepointLength(text: string): number {
  let count = 0;
  for (let i = 0; i < text.length; ) {
    const code = text.codePointAt(i) as number;
    i += code > 0xffff ? 2 : 1;
    count += 1;
  }
  return count;
}

/** 取码点区间 [start, end) 的精确切片，start/end 会夹到合法范围。 */
export function sliceByCodepoints(text: string, start: number, end: number): string {
  const from = codepointOffsetToUtf16(text, Math.min(start, end));
  const to = codepointOffsetToUtf16(text, Math.max(start, end));
  return text.slice(from, to);
}

/**
 * 找出 needle 在 text 中每次出现的“码点起始偏移”。
 * 用于用户手动编辑引文时选择第几次出现；不使用 indexOf 的首个命中冒充选择结果。
 */
export function findOccurrenceOffsets(text: string, needle: string): number[] {
  if (!needle) return [];
  const offsets: number[] = [];
  let from = 0;
  for (;;) {
    const at = text.indexOf(needle, from);
    if (at < 0) break;
    offsets.push(utf16OffsetToCodepoint(text, at));
    from = at + 1; // 允许重叠出现（如 “aaa” 中的 “aa”）
    if (offsets.length > 10_000) break;
  }
  return offsets;
}

/** 第 n 次（1 起）出现的码点偏移；不存在返回 null。 */
export function occurrenceOffset(text: string, needle: string, occurrence: number): number | null {
  if (occurrence < 1) return null;
  const all = findOccurrenceOffsets(text, needle);
  return occurrence <= all.length ? all[occurrence - 1] : null;
}

/** 该偏移处的切片是否与引文逐字一致（保存前自检）。 */
export function sliceMatchesQuote(text: string, start: number, quote: string): boolean {
  if (!quote) return false;
  return sliceByCodepoints(text, start, start + codepointLength(quote)) === quote;
}