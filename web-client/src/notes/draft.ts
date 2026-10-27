import type { NoteChanges, NotePage, NoteWrite } from './types';

type Job = { patch: NoteChanges; revision: number; id: string };
export type SaveState = 'saved' | 'dirty' | 'saving' | 'error' | 'conflict';

/** One transaction stream per page. Failed requests retain their original id. */
export class PageDraft {
  private base: NotePage;
  private pending: NoteChanges = {};
  private job: Job | null = null;
  private failed: Job | null = null;
  private busy: Promise<boolean> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private dead = false;
  error: Error | null = null;
  composing = false;

  constructor(page: NotePage, private write: (patch: NoteWrite) => Promise<NotePage>, private notify: () => void) {
    this.base = page;
  }
  get view(): NotePage { return { ...this.base, ...this.failed?.patch, ...this.job?.patch, ...this.pending }; }
  get dirty(): boolean { return !!this.job || !!this.failed || Object.keys(this.pending).length > 0; }
  get status(): SaveState {
    if (this.failed) return (this.error as Error & { status?: number })?.status === 409 ? 'conflict' : 'error';
    if (this.job) return 'saving';
    return Object.keys(this.pending).length ? 'dirty' : 'saved';
  }
  private emit() { if (!this.dead) this.notify(); }
  listen(notify: () => void) { this.notify = notify; }
  patch(value: NoteChanges) {
    if (this.dead) return;
    this.pending = { ...this.pending, ...value }; this.emit(); this.schedule();
  }
  schedule() {
    if (this.timer) clearTimeout(this.timer);
    if (!this.failed && !this.composing) this.timer = setTimeout(() => { void this.flush(); }, 800);
  }
  private async submit(job: Job): Promise<boolean> {
    this.job = job; this.failed = null; this.error = null; this.emit();
    const operation = (async () => {
      try {
        const result = await this.write({ ...job.patch, expected_revision: job.revision, request_id: job.id });
        if (this.dead) return false;
        this.base = result;
        return true;
      } catch (error) {
        this.failed = job; this.error = error instanceof Error ? error : new Error(String(error));
        return false;
      } finally {
        this.job = null; this.busy = null; this.emit();
      }
    })();
    this.busy = operation;
    const ok = await operation;
    return ok && !this.dead && Object.keys(this.pending).length ? this.flush() : ok;
  }
  async flush(): Promise<boolean> {
    if (this.dead || this.composing || this.failed) return false;
    if (this.timer) clearTimeout(this.timer);
    if (this.busy) { if (!await this.busy) return false; return this.flush(); }
    if (!Object.keys(this.pending).length) return true;
    const patch = this.pending; this.pending = {};
    return this.submit({ patch, revision: this.base.revision, id: crypto.randomUUID() });
  }
  async retry(): Promise<boolean> {
    if (!this.failed || this.status === 'conflict') return false;
    return this.submit(this.failed);
  }
  rebase(latest: NotePage, keepDraft: boolean) {
    if (this.busy) return;
    const patch = { ...this.failed?.patch, ...this.pending };
    this.base = latest; this.failed = null; this.error = null; this.pending = keepDraft ? patch : {}; this.emit();
  }
  dispose() { this.dead = true; if (this.timer) clearTimeout(this.timer); }
}
