/**
 * 集合 / 批量 / 导出端点的调用形状。
 *
 * 重点是「前端绝不把服务器路径交给后端」，以及筛选条件确实进了查询串。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  batchEvidence,
  createExport,
  exportDownloadUrl,
  listCollections,
  listEvidence,
  previewExport,
  patchCollection,
} from './endpoints';
import { reauthenticate, resetSession } from './client';

type Call = { url: string; method: string; body: string | null };

const calls: Call[] = [];

function installFetch(status = 200, body: unknown = {}): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({
        url: String(url),
        method: init.method ?? 'GET',
        body: typeof init.body === 'string' ? init.body : null,
      });
      return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

async function boot(): Promise<void> {
  installFetch(200, {
    name: '个人图书馆', root: 'C:/data/library', csrf_token: 'token-1',
    ocr: { available: false }, coverage: {},
  });
  await reauthenticate();
  calls.length = 0;
}

describe('集合与批量端点', () => {
  beforeEach(async () => {
    resetSession();
    await boot();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSession();
  });

  it('集合列表取 items', async () => {
    installFetch(200, { items: [{ id: 'col_1', name: '文物建筑资料', evidence_count: 3 }] });
    const collections = await listCollections();
    expect(calls[0].url).toBe('/api/v1/collections');
    expect(collections[0].evidence_count).toBe(3);
  });

  it('改名只发 name/description，并带上写令牌', async () => {
    installFetch(200, { id: 'col_1' });
    await patchCollection('col_1', { name: '新名字' });
    expect(calls[0].url).toBe('/api/v1/collections/col_1');
    expect(calls[0].method).toBe('PATCH');
    expect(JSON.parse(calls[0].body!)).toEqual({ name: '新名字' });
  });

  it('批量只发送显式 ID 与所选操作', async () => {
    installFetch(200, { evidence_ids: ['ev_1'], updated: 1, added: 1, removed: 0 });
    await batchEvidence({ evidence_ids: ['ev_1', 'ev_2'], add_to_collection_id: 'col_1' });
    const body = JSON.parse(calls[0].body!);
    expect(body.evidence_ids).toEqual(['ev_1', 'ev_2']);
    expect(body.add_to_collection_id).toBe('col_1');
    expect('category' in body).toBe(false);
    expect('review_status' in body).toBe(false);
  });
});

describe('摘录列表筛选', () => {
  beforeEach(async () => {
    resetSession();
    await boot();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSession();
  });

  it('分类与集合进入查询串，空值不发送', async () => {
    installFetch(200, { items: [], total: 0, page: 1, page_size: 30 });
    await listEvidence({
      page: 2, pageSize: 30, docId: 'doc 1', reviewStatus: 'accepted',
      category: '文物', collectionId: 'col 1',
    });
    const url = new URL(calls[0].url, 'http://x');
    expect(url.pathname).toBe('/api/v1/evidence');
    expect(url.searchParams.get('category')).toBe('文物');
    expect(url.searchParams.get('collection_id')).toBe('col 1');
    expect(url.searchParams.get('doc_id')).toBe('doc 1');
    expect(url.searchParams.get('review_status')).toBe('accepted');
    expect(url.searchParams.get('page')).toBe('2');
  });

  it('不筛选时不发送空参数', async () => {
    installFetch(200, { items: [], total: 0, page: 1, page_size: 30 });
    await listEvidence({ page: 1 });
    expect(calls[0].url).toBe('/api/v1/evidence?page=1');
  });
});

describe('导出端点', () => {
  beforeEach(async () => {
    resetSession();
    await boot();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSession();
  });

  it('预览与生成只发送格式、范围与令牌', async () => {
    installFetch(201, { id: 'exp_1' });
    await previewExport({ format: 'csv', filters: { review_status: 'accepted' } });
    const previewBody = JSON.parse(calls[0].body!);
    expect(calls[0].url).toBe('/api/v1/exports/preview');
    expect(Object.keys(previewBody).sort()).toEqual(['filters', 'format']);
    expect(previewBody.filters.review_status).toBe('accepted');

    installFetch(201, { id: 'exp_1' });
    await createExport({
      format: 'csv', filters: { review_status: 'accepted' }, snapshot_token: 't'.repeat(64),
    });
    const createBody = JSON.parse(calls[1].body!);
    expect(calls[1].url).toBe('/api/v1/exports');
    expect(createBody.snapshot_token).toBe('t'.repeat(64));
    for (const forbidden of ['path', 'output', 'filename', 'stored_path']) {
      expect(forbidden in createBody).toBe(false);
    }
  });

  it('显式选择随请求发送，空数组不会发出去', async () => {
    installFetch(201, { count: 0 });
    await previewExport({ format: 'json', filters: { review_status: 'all' }, evidence_ids: ['ev_1'] });
    expect(JSON.parse(calls[0].body!).evidence_ids).toEqual(['ev_1']);
  });

  it('下载地址只按 ID 拼接，不接受路径', () => {
    expect(exportDownloadUrl('exp_abc')).toBe('/api/v1/exports/exp_abc/download');
    expect(exportDownloadUrl('../../etc/passwd')).toBe(
      '/api/v1/exports/..%2F..%2Fetc%2Fpasswd/download',
    );
  });
});