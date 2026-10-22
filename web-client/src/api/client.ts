/**
 * 同源 API 客户端。
 *
 * - 基址 /api/v1；开发时由 Vite 代理到 http://127.0.0.1:8765，生产环境同源相对路径。
 * - 所有请求带 credentials: 'same-origin'，会话 cookie 由 GET /workspace 建立。
 * - csrf_token 只保存在模块内存里，不写入 localStorage / sessionStorage / URL。
 * - 写请求（POST / PATCH / DELETE）附带 X-CSRF-Token。
 */
import type { ApiErrorBody } from './types';

const BASE = '/api/v1';
const WRITE_METHODS = new Set(['POST', 'PATCH', 'PUT', 'DELETE']);
const GET_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** 版本/代次类冲突：UI 必须保留草稿。 */
  get isConflict(): boolean {
    return this.status === 409;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** 无法连接本地服务。 */
  get isOffline(): boolean {
    return this.status === 0;
  }
}

let csrfToken: string | null = null;
let bootPromise: Promise<unknown> | null = null;
/** 服务重启后的重新握手：并发的 401 共用同一个进行中的请求，避免互相清空令牌。 */
let reauthPromise: Promise<unknown> | null = null;

export function peekCsrfToken(): string | null {
  return csrfToken;
}

function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

function parseErrorBody(status: number, body: unknown): ApiError {
  const envelope = body as Partial<ApiErrorBody> | null;
  const err = envelope && typeof envelope === 'object' ? envelope.error : undefined;
  if (err && typeof err === 'object') {
    return new ApiError(
      status,
      typeof err.code === 'string' ? err.code : `HTTP_${status}`,
      typeof err.message === 'string' && err.message ? err.message : `请求失败（HTTP ${status}）`,
      err.details,
    );
  }
  return new ApiError(status, `HTTP_${status}`, `请求失败（HTTP ${status}）`);
}

async function readBody(response: Response): Promise<unknown> {
  const type = response.headers.get('content-type') ?? '';
  if (!type.includes('json')) {
    const text = await response.text();
    return text ? { error: { code: `HTTP_${response.status}`, message: text.slice(0, 400) } } : null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function combineSignals(external: AbortSignal | undefined, timeoutMs: number | null): {
  signal: AbortSignal;
  cleanup: () => void;
} {
  const controller = new AbortController();
  const timer =
    timeoutMs === null ? null : window.setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs);
  const onAbort = () => controller.abort(external?.reason);
  if (external) {
    if (external.aborted) controller.abort(external.reason);
    else external.addEventListener('abort', onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    cleanup: () => {
      if (timer !== null) window.clearTimeout(timer);
      if (external) external.removeEventListener('abort', onAbort);
    },
  };
}

async function rawRequest<T>(
  method: string,
  path: string,
  init: { body?: unknown; signal?: AbortSignal; retryUnauthorized?: boolean } = {},
): Promise<T> {
  const { signal, cleanup } = combineSignals(init.signal, method === 'GET' ? GET_TIMEOUT_MS : null);
  const headers = new Headers({ Accept: 'application/json' });
  let body: BodyInit | undefined;
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(init.body);
  }
  if (WRITE_METHODS.has(method)) {
    if (!csrfToken) throw new ApiError(0, 'NO_SESSION', '本地会话尚未建立，请刷新页面重试。');
    headers.set('X-CSRF-Token', csrfToken);
  }

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body,
      signal: signal as AbortSignal,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch (cause) {
    cleanup();
    if (init.signal?.aborted) throw cause;
    if (cause instanceof DOMException && cause.name === 'TimeoutError') {
      throw new ApiError(0, 'TIMEOUT', '本地服务响应超时。');
    }
    throw new ApiError(0, 'NETWORK', '无法连接本地文献服务，请确认启动器仍在运行。');
  }

  const payload = await readBody(response);
  cleanup();

  if (!response.ok) {
    const error = parseErrorBody(response.status, payload);
    // 服务重启后内存会话与 csrf 令牌都会丢失：先清掉旧令牌再重新握手，然后重试一次。
    // 并发的 401 共用同一个握手结果，不会互相覆盖。
    if (response.status === 401 && init.retryUnauthorized !== false && path !== '/workspace') {
      await reauthenticate();
      return rawRequest<T>(method, path, { ...init, retryUnauthorized: false });
    }
    throw error;
  }
  return payload as T;
}

/** GET /workspace：建立会话 cookie 并取回 csrf_token（只存内存）。 */
async function fetchWorkspace<T>(): Promise<T> {
  const response = await fetch(`${BASE}/workspace`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  });
  const payload = await readBody(response);
  if (!response.ok) throw parseErrorBody(response.status, payload);
  const token = (payload as { csrf_token?: unknown } | null)?.csrf_token;
  setCsrfToken(typeof token === 'string' && token ? token : null);
  if (!csrfToken) throw new ApiError(response.status, 'NO_CSRF', '本地服务未返回 csrf_token，无法写入。');
  return payload as T;
}

/**
 * 丢弃可能已失效的会话与令牌，然后重新握手。
 * 首屏只调用一次；401 重试走这里，因此服务重启后拿到的一定是新令牌。
 */
export function reauthenticate(): Promise<unknown> {
  if (reauthPromise) return reauthPromise;
  setCsrfToken(null);
  bootPromise = null;
  reauthPromise = fetchWorkspace<unknown>().finally(() => {
    reauthPromise = null;
  });
  return reauthPromise;
}

export function bootWorkspace<T>(): Promise<T> {
  if (!bootPromise) {
    bootPromise = fetchWorkspace<T>().catch((error: unknown) => {
      bootPromise = null;
      throw error;
    });
  }
  return bootPromise as Promise<T>;
}

export function resetSession(): void {
  setCsrfToken(null);
  bootPromise = null;
  reauthPromise = null;
}

export function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return rawRequest<T>('GET', path, { signal });
}

export function apiPost<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return rawRequest<T>('POST', path, { body: body ?? {}, signal });
}

export function apiPatch<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return rawRequest<T>('PATCH', path, { body, signal });
}

/** 二进制资源 URL（PNG / 原件下载）。直接交给 <img> 或 <a>，不经过 JS。 */
export function apiAssetUrl(path: string): string {
  return `${BASE}${path}`;
}