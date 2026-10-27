import { describe, expect, it, vi } from 'vitest';
import { PageDraft } from './draft';

const page = { id: 'p1', title: '原稿', revision: 1, body: { schema_version: 1, blocks: [] }, tags: [] };
const deferred = () => { let resolve!: (value: any) => void; let reject!: (error: Error) => void;
  const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

describe('serialized page editing', () => {
  it('keeps later typing while a save is in flight and uses the returned revision', async () => {
    const first = deferred();
    const write = vi.fn().mockReturnValueOnce(first.promise).mockImplementation(async (patch) => ({ ...page, ...patch, revision: 3 }));
    const draft = new PageDraft(page as any, write, () => {});
    draft.patch({ title: '第一版' }); const saved = draft.flush();
    draft.patch({ title: '第二版' });
    expect(draft.view.title).toBe('第二版');
    first.resolve({ ...page, title: '第一版', revision: 2 });
    expect(await saved).toBe(true);
    expect(write).toHaveBeenCalledTimes(2);
    expect(write.mock.calls[1][0].expected_revision).toBe(2);
    expect(draft.view.title).toBe('第二版');
    expect(draft.status).toBe('saved'); draft.dispose();
  });
  it('retries the exact failed request before saving later edits', async () => {
    const write = vi.fn().mockRejectedValueOnce(new Error('offline')).mockImplementation(async (patch) => ({ ...page, ...patch, revision: patch.expected_revision + 1 }));
    const draft = new PageDraft(page as any, write, () => {});
    draft.patch({ title: '离线草稿' }); expect(await draft.flush()).toBe(false);
    draft.patch({ title: '继续编辑' });
    expect(draft.view.title).toBe('继续编辑');
    expect(await draft.retry()).toBe(true);
    expect(write.mock.calls[1][0]).toEqual(write.mock.calls[0][0]);
    expect(draft.view.title).toBe('继续编辑'); draft.dispose();
  });
  it('preserves a conflict without silently retrying or replacing the draft', async () => {
    const error = Object.assign(new Error('changed'), { status: 409 });
    const write = vi.fn().mockRejectedValue(error);
    const draft = new PageDraft(page as any, write, () => {});
    draft.patch({ title: '我的草稿' }); await draft.flush();
    expect(draft.status).toBe('conflict'); expect(draft.view.title).toBe('我的草稿');
    expect(await draft.flush()).toBe(false); expect(write).toHaveBeenCalledTimes(1);
    draft.rebase({ ...page, title: '服务器版', revision: 2 } as any, false);
    expect(draft.view.title).toBe('服务器版'); expect(draft.status).toBe('saved'); draft.dispose();
  });
  it('does not submit during IME composition', async () => {
    const write = vi.fn(); const draft = new PageDraft(page as any, write, () => {});
    draft.composing = true; draft.patch({ title: '输入中' });
    expect(await draft.flush()).toBe(false); expect(write).not.toHaveBeenCalled(); draft.dispose();
  });
  it('ignores an old page response after disposal', async () => {
    const response = deferred(); const notify = vi.fn();
    const draft = new PageDraft(page as any, () => response.promise, notify);
    draft.patch({ title: '旧页' }); const pending = draft.flush(); draft.dispose(); notify.mockClear();
    response.resolve({ ...page, title: '旧页', revision: 2 }); await pending;
    expect(notify).not.toHaveBeenCalled();
  });
});
