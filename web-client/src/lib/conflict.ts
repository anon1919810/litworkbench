/**
 * 冲突与错误的统一解释。
 *
 * 契约里两类 409 的处理方式不同：
 * - SEARCH_INDEX_CHANGED：保留检索条件，回到第一页重查；
 * - 其它 409（版本哈希冲突）：保留用户草稿，要求重新核对。
 */
import { ApiError } from '../api/client';

export type ErrorKind = 'index-changed' | 'version-conflict' | 'offline' | 'http' | 'aborted' | 'unknown';

export interface ErrorInfo {
  kind: ErrorKind;
  status: number;
  code: string;
  message: string;
  /** UI 必须保留草稿/选择，不要清空 */
  keepDraft: boolean;
}

export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException) return error.name === 'AbortError' || error.name === 'TimeoutError';
  return error instanceof Error && error.name === 'AbortError';
}

/** 把任意异常翻译成 UI 可直接使用的描述。 */
export function describeError(error: unknown): ErrorInfo {
  if (isAbortError(error)) {
    return { kind: 'aborted', status: 0, code: 'ABORTED', message: '请求已取消。', keepDraft: true };
  }
  if (error instanceof ApiError) {
    if (error.code === 'SEARCH_INDEX_CHANGED' || (error.status === 409 && error.code === 'INDEX_CHANGED')) {
      return {
        kind: 'index-changed',
        status: error.status,
        code: error.code,
        message: '资料已更新（索引代次变化），已保留检索条件并回到第一页重查。',
        keepDraft: true,
      };
    }
    if (error.status === 409) {
      return {
        kind: 'version-conflict',
        status: error.status,
        code: error.code,
        message: error.message || '内容已被其他改动更新，请重新核对后再保存。',
        keepDraft: true,
      };
    }
    if (error.status === 0) {
      return {
        kind: 'offline',
        status: 0,
        code: error.code,
        message: error.message,
        keepDraft: true,
      };
    }
    return { kind: 'http', status: error.status, code: error.code, message: error.message, keepDraft: true };
  }
  const message = error instanceof Error ? error.message : String(error);
  return { kind: 'unknown', status: 0, code: 'UNKNOWN', message, keepDraft: true };
}

export interface PagingState {
  page: number;
  expectedIndexRevision: number | null;
}

export interface PagingResolution {
  next: PagingState;
  notice: string;
}

/** 检索分页遇到索引代次冲突：保留条件，回到第一页，不带旧代次重查。 */
export function resolvePagingAfterConflict(error: unknown): PagingResolution | null {
  const info = describeError(error);
  if (info.kind !== 'index-changed') return null;
  return {
    next: { page: 1, expectedIndexRevision: null },
    notice: info.message,
  };
}

/** 是否为“本页内容已被改动”的冲突（校订、摘录、印刷页码）。 */
export function isVersionConflict(error: unknown): boolean {
  return describeError(error).kind === 'version-conflict';
}