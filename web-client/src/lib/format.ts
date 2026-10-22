/**
 * 标签与数字格式。后端没有给出的词汇不做臆造：未收录的状态直接显示原值。
 */
import type { JobState, PrintedPage, PrintedPageStatus, ReviewStatus, TextVersion } from '../api/types';

export type Tone = 'neutral' | 'info' | 'warn' | 'error' | 'progress';

const numberFormat = new Intl.NumberFormat('zh-CN');

export function num(value: number | null | undefined, fallback = '—'): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return fallback;
  return numberFormat.format(value);
}

/**
 * 文件页 = 文件内第几页（从 1 开始）；与印刷页码分开记录。
 * 总页数未知时只说“文件第 N 页”——不谎称“统计中”，也不猜总页数。
 */
export function filePageLabel(filePage: number | null, pageCount: number | null): string {
  if (filePage === null || filePage === undefined) return '文件页不适用（文档单元）';
  const total = pageCount === null || pageCount === undefined ? null : pageCount;
  return total === null ? `文件第 ${num(filePage)} 页` : `文件第 ${num(filePage)} 页 / 共 ${num(total)} 页`;
}

export function printedPageStatusLabel(status: PrintedPageStatus | null | undefined): string | null {
  if (!status || status === 'none') return null;
  if (status === 'candidate') return '候选';
  if (status === 'confirmed') return '已确认';
  return status;
}

/**
 * 印刷页码是字符串（可能是阿拉伯数字、罗马数字或中文标注），按原样显示。
 * 空字符串或 null 都表示尚未填写。
 */
export function printedPageLabel(printed: PrintedPage | undefined, status: PrintedPageStatus | null | undefined): string {
  const state = printedPageStatusLabel(status);
  const value = typeof printed === 'string' ? printed.trim() : '';
  if (!value) return state ? `印刷页码未填（${state}）` : '印刷页码未填';
  return state ? `印刷页码 ${value}（${state}）` : `印刷页码 ${value}（未确认）`;
}

/** 文本版本：original = 识别原文，corrected = 人工校订后的文本。 */
export function versionLabel(version: TextVersion | null | undefined): string {
  if (!version) return '未知版本';
  if (version === 'corrected') return '校订版';
  if (version === 'original') return '识别原文';
  return String(version);
}

/** 是否值得提示“这条文字来自校订版”。 */
export function isCorrectedVersion(version: TextVersion | null | undefined): boolean {
  return version === 'corrected';
}

const JOB_STATE: Record<string, { label: string; tone: Tone }> = {
  queued: { label: '排队中', tone: 'neutral' },
  preparing: { label: '准备中', tone: 'neutral' },
  running: { label: '处理中', tone: 'progress' },
  pausing: { label: '正在暂停', tone: 'warn' },
  paused: { label: '已暂停', tone: 'neutral' },
  partial: { label: '部分完成', tone: 'warn' },
  completed: { label: '已完成', tone: 'info' },
  failed: { label: '失败', tone: 'error' },
};

export function jobStateInfo(state: JobState): { label: string; tone: Tone } {
  return JOB_STATE[state] ?? { label: String(state), tone: 'neutral' };
}

/**
 * 任务逐页检查点的状态词（GET /jobs/{id}/units）：
 * work=已登记未处理，done=处理完成且拿到文字，pending=处理了但没有文字，failed=出错。
 */
export function jobUnitStateInfo(state: string | null | undefined): { label: string; tone: Tone } {
  switch (state) {
    case 'work':
      return { label: '未处理', tone: 'neutral' };
    case 'done':
      return { label: '已完成', tone: 'info' };
    case 'pending':
      return { label: '无文字待确认', tone: 'warn' };
    case 'failed':
      return { label: '失败', tone: 'error' };
    default:
      return { label: state ? String(state) : '—', tone: 'neutral' };
  }
}

/**
 * 检查点的位置标签。position 是 0 起的任务内序号，界面按人的习惯显示第 N 项；
 * 只有 PDF 文献才叫“文件第 N 页”，Word 的段落/表格单元不冒充书的物理页码。
 */
export function jobUnitLabel(filetype: string | null | undefined, position: number | null | undefined): string {
  const index = typeof position === 'number' && Number.isFinite(position) ? position + 1 : null;
  if (index === null) return '位置未知';
  const kind = (filetype ?? '').toLowerCase();
  return kind.includes('pdf') ? `文件第 ${num(index)} 页` : `文档单元 ${num(index)}`;
}

/** 检查点对应的单元是否已经生成：未处理的页可能还没有单元行，不能打开阅读区。 */
export function jobUnitOpenable(state: string | null | undefined): boolean {
  return state !== 'work';
}

export function isActiveJob(state: JobState): boolean {
  return state === 'queued' || state === 'preparing' || state === 'running' || state === 'pausing';
}

export const REVIEW_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: '待核验', tone: 'warn' },
  accepted: { label: '已接受', tone: 'info' },
  rejected: { label: '已拒绝', tone: 'error' },
  stale: { label: '需复核', tone: 'warn' },
};

export function reviewStatusInfo(status: ReviewStatus | string): { label: string; tone: Tone } {
  return REVIEW_STATUS[status] ?? { label: String(status), tone: 'neutral' };
}

/** 单元与文献的状态词来自本机服务；未收录的值直接显示原值，不臆造中文。 */
const UNIT_STATUS: Record<string, { label: string; tone: Tone }> = {
  completed: { label: '已完成', tone: 'info' },
  pending: { label: '待处理', tone: 'warn' },
  failed: { label: '失败', tone: 'error' },
  importing: { label: '导入中', tone: 'neutral' },
  empty: { label: '无文本单元', tone: 'neutral' },
  partial: { label: '部分完成', tone: 'warn' },
};

export function unitStatusInfo(status: string | null | undefined): { label: string; tone: Tone } {
  if (!status) return { label: '—', tone: 'neutral' };
  return UNIT_STATUS[status] ?? { label: String(status), tone: 'neutral' };
}

export function filetypeLabel(filetype: string | null | undefined): string {
  if (!filetype) return '—';
  const value = filetype.toLowerCase();
  if (value.includes('pdf')) return 'PDF';
  if (value.includes('docx') || value.includes('word') || value.includes('doc')) return 'Word';
  return filetype.toUpperCase();
}

/** 处理方式原样展示（本地服务给出的 method 值），不做翻译臆造。 */
export function methodLabel(method: string | null | undefined): string {
  if (!method) return '处理方式未记录';
  const value = method.toLowerCase();
  const known: Record<string, string> = {
    text: '文本层',
    text_layer: '文本层',
    ocr: 'OCR 识别',
    force: '强制 OCR',
    mixed: '混合',
    docx: 'Word 文本',
    manual: '人工录入',
    import: '文献导入',
    reprocess: '重新识别',
    ocr_candidate: '生成候选',
    index: '更新检索',
  };
  return known[value] ?? method;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatPercent(part: number, whole: number): number {
  if (!whole) return 0;
  return Math.min(100, Math.round((part / whole) * 100));
}
