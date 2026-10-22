/**
 * 摘录草稿的纯逻辑：把浏览器选区（UTF-16）换算成契约要求的码点偏移，
 * 并校验“偏移处的切片”与引文逐字一致。保存前必须通过这里的校验。
 */
import { codepointLength, findOccurrenceOffsets, sliceByCodepoints, sliceMatchesQuote, utf16OffsetToCodepoint } from './unicode';
import type { SelectedSlice } from './selection';

export interface QuoteDraft {
  quote: string;
  /** 契约口径：Unicode 码点偏移 */
  startOffset: number;
}

export interface QuoteResolution {
  valid: boolean;
  error: string | null;
  /** 引文在该页出现的全部码点偏移 */
  occurrences: number[];
  /** 当前 startOffset 是否正好落在某一次出现上 */
  matched: boolean;
  endOffset: number;
}

/** 由选区生成草稿；偏移换算在请求发出之前完成。 */
export function draftFromSelection(text: string, selection: SelectedSlice): QuoteDraft | null {
  const quote = selection.text;
  if (!quote) return null;
  const startOffset = utf16OffsetToCodepoint(text, selection.start);
  if (!sliceMatchesQuote(text, startOffset, quote)) return null;
  return { quote, startOffset };
}

/** 校验草稿；引文为空、偏移越界或与原文不符时给出可执行的中文提示。 */
export function resolveQuoteDraft(text: string, draft: QuoteDraft): QuoteResolution {
  const occurrences = findOccurrenceOffsets(text, draft.quote);
  const endOffset = draft.startOffset + codepointLength(draft.quote);
  if (!draft.quote.trim()) {
    return { valid: false, error: '引文不能为空，请重新选择或填写文字。', occurrences, matched: false, endOffset };
  }
  const matched = sliceMatchesQuote(text, draft.startOffset, draft.quote);
  if (!matched) {
    const known = occurrences.length > 0 ? `本页该文字共出现 ${occurrences.length} 次。` : '本页没有这段文字。';
    return {
      valid: false,
      error: `偏移 ${draft.startOffset} 处的原文与引文不一致。${known}请重新选择或选择正确的出现次序。`,
      occurrences,
      matched: false,
      endOffset,
    };
  }
  return { valid: true, error: null, occurrences, matched, endOffset };
}

/** 引文被编辑后重新定位：保留仍成立的那次出现，否则回到第一次出现并要求用户确认。 */
export function relinkDraft(text: string, draft: QuoteDraft): QuoteDraft {
  const occurrences = findOccurrenceOffsets(text, draft.quote);
  if (occurrences.includes(draft.startOffset)) return draft;
  return occurrences.length ? { quote: draft.quote, startOffset: occurrences[0] } : draft;
}

/** 取该偏移处引文的上下文，用于在对话框里显示定位依据。 */
export function quoteContext(text: string, draft: QuoteDraft, radius = 24): { before: string; after: string } {
  const start = draft.startOffset;
  const end = start + codepointLength(draft.quote);
  return {
    before: sliceByCodepoints(text, Math.max(0, start - radius), start),
    after: sliceByCodepoints(text, end, Math.min(codepointLength(text), end + radius)),
  };
}