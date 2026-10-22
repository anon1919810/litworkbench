import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiPost, peekCsrfToken, reauthenticate, resetSession } from './client';

type Call = { url: string; method: string; token: string | null; body: string | null };

const calls: Call[] = [];

/** 受控的 fetch 替身：按脚本返回响应。 */
function installFetch(script: Array<{ status: number; body: unknown }>): void {
  let index = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      calls.push({
        url: String(url),
        method: init.method ?? 'GET',
        token: headers.get('X-CSRF-Token'),
        body: typeof init.body === 'string' ? init.body : null,
      });
      const step = script[Math.min(index, script.length - 1)];
      index += 1;
      return new Response(step.status === 200 || step.status === 202 ? JSON.stringify(step.body) : JSON.stringify(step.body), {
        status: step.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

const workspaceBody = (token: string) => ({
  name: '个人图书馆',
  root: 'C:/data/library',
  csrf_token: token,
  ocr: { available: true, detail: 'rapidocr' },
  coverage: {
    known_pdf_pages: 91,
    covered_pdf_pages: 88,
    pending_pdf_pages: 3,
    failed_pdf_pages: 0,
    unknown_documents: 0,
    indexed_docx: 2,
    total_docx: 2,
  },
});

describe('会话与 csrf 令牌', () => {
  beforeEach(() => {
    calls.length = 0;
    resetSession();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetSession();
  });

  it('401 之后重新握手，重试请求带上的是新令牌', async () => {
    installFetch([
      { status: 200, body: workspaceBody('token-old') }, // 首次握手
      { status: 401, body: { error: { code: 'SESSION_EXPIRED', message: '会话已失效' } } },
      { status: 200, body: workspaceBody('token-new') }, // 401 触发的重新握手
      { status: 202, body: { id: 'ev-1', quote: '文庙' } },
    ]);

    await reauthenticate();
    expect(peekCsrfToken()).toBe('token-old');

    const result = await apiPost<{ id: string }>('/evidence', { quote: '文庙' });
    expect(result.id).toBe('ev-1');

    const writeCalls = calls.filter((call) => call.method === 'POST');
    expect(writeCalls.length).toBe(2);
    // 第一次写请求带的是已失效的旧令牌，重试时必须换成重新握手拿到的新令牌
    expect(writeCalls[0].token).toBe('token-old');
    expect(writeCalls[1].token).toBe('token-new');
    expect(peekCsrfToken()).toBe('token-new');
  });

  it('旧令牌不会被复用：重新握手后 peek 到的是新值', async () => {
    installFetch([
      { status: 200, body: workspaceBody('token-old') },
      { status: 200, body: workspaceBody('token-new') },
    ]);
    await reauthenticate();
    expect(peekCsrfToken()).toBe('token-old');
    await reauthenticate();
    expect(peekCsrfToken()).toBe('token-new');
  });

  it('并发的重新握手共用一次请求，不会互相清空令牌', async () => {
    installFetch([{ status: 200, body: workspaceBody('token-shared') }]);
    await Promise.all([reauthenticate(), reauthenticate(), reauthenticate()]);
    const workspaceCalls = calls.filter((call) => call.url.endsWith('/workspace'));
    expect(workspaceCalls).toHaveLength(1);
    expect(peekCsrfToken()).toBe('token-shared');
  });

  it('重试后仍失败则抛出解析后的错误，而不是静默成功', async () => {
    installFetch([
      { status: 200, body: workspaceBody('token-old') },
      { status: 401, body: { error: { code: 'SESSION_EXPIRED', message: '会话已失效' } } },
      { status: 200, body: workspaceBody('token-new') },
      { status: 401, body: { error: { code: 'SESSION_EXPIRED', message: '会话已失效' } } },
    ]);
    await reauthenticate();
    await expect(apiPost('/evidence', { quote: '文庙' })).rejects.toMatchObject({ status: 401, code: 'SESSION_EXPIRED' });
  });

  it('没有会话时写请求直接失败，不发请求', async () => {
    installFetch([{ status: 200, body: workspaceBody('token-new') }]);
    await expect(apiPost('/evidence', {})).rejects.toMatchObject({ code: 'NO_SESSION' });
    expect(calls).toHaveLength(0);
  });
});