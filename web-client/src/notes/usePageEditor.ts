import { useEffect, useRef, useState } from 'react';
import { getPage, savePage } from './api';
import { PageDraft } from './draft';

const drafts = new Map<string, PageDraft>();
if (typeof window !== 'undefined') window.addEventListener('beforeunload', (event) => {
  if ([...drafts.values()].some((draft) => draft.dirty)) { event.preventDefault(); event.returnValue = ''; }
});

/** Dirty page sessions survive changing views; only confirmed saves replace a base. */
export function usePageEditor(id: string | null, onSaved: () => void) {
  const [draft, setDraft] = useState<PageDraft | null>(null);
  const [error, setError] = useState('');
  const [epoch, setEpoch] = useState(0);
  const [, redraw] = useState(0);
  const savedCallback = useRef(onSaved); savedCallback.current = onSaved;
  useEffect(() => {
    let alive = true; let current: PageDraft | null = null; let revision = -1;
    setDraft(null); setError('');
    if (!id) return;
    const notify = () => {
      if (!alive || !current) return;
      redraw((value) => value + 1);
      if (current.status === 'saved' && current.view.revision !== revision) {
        revision = current.view.revision; savedCallback.current();
      }
    };
    const attach = (value: PageDraft) => {
      current = value; revision = value.view.revision; value.listen(notify);
      drafts.set(id, value); setDraft(value); setEpoch((value) => value + 1);
    };
    const cached = drafts.get(id);
    if (cached?.dirty) attach(cached);
    else void getPage(id).then((page) => {
      if (alive) attach(new PageDraft(page, (patch) => savePage(id, patch), notify));
    }).catch((cause: Error) => { if (alive) setError(cause.message); });
    const timer = setInterval(() => {
      if (!current || current.dirty || document.visibilityState === 'hidden') return;
      const target = current;
      void getPage(id).then((latest) => {
        if (alive && !target.dirty && latest.revision !== target.view.revision) {
          target.rebase(latest, false); setEpoch((value) => value + 1);
        }
      }).catch(() => {}); // A failed auxiliary refresh never discards the current page.
    }, 10_000);
    return () => {
      alive = false; clearInterval(timer); current?.listen(() => {});
      if (current && !current.dirty) { current.dispose(); drafts.delete(id); }
    };
  }, [id]);
  const reload = async (keepDraft = false) => {
    if (!id || !draft) return;
    const latest = await getPage(id); draft.rebase(latest, keepDraft);
    if (!keepDraft) setEpoch((value) => value + 1);
    else await draft.flush();
  };
  return { draft, error, epoch, reload };
}
