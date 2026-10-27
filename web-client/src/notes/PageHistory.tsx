import { useEffect, useState } from 'react';
import { history } from './api';
import type { NoteHistory } from './types';

export function PageHistory({ id, readonly, restore, close }: {
  id: string; readonly: boolean; restore: (revision: number) => void; close: () => void;
}) {
  const [items, setItems] = useState<NoteHistory[]>([]); const [error, setError] = useState('');
  const [page, setPage] = useState(1); const [total, setTotal] = useState(0);
  useEffect(() => { let alive = true; void history(id, page).then((data) => { if (alive) { setItems(data.items); setTotal(data.total); } })
    .catch((cause: Error) => { if (alive) setError(cause.message); }); return () => { alive = false; }; }, [id, page]);
  return <aside className="note-history" aria-label="修改历史">
    <div className="note-panel-heading"><h2>修改历史</h2><button type="button" className="btn btn-sm" onClick={close}>关闭</button></div>
    <p className="help">恢复会创建一个新版本，已有记录继续保留。</p>
    {error ? <p role="alert">{error}</p> : null}
    {items.map((item) => <article key={item.revision}>
      <strong>版本 {item.revision} · {item.snapshot.title}</strong>
      <small>{item.origin === 'codex' ? 'Codex' : '网页'} · {new Date(item.created_at).toLocaleString()}</small>
      <pre>{JSON.stringify(item.snapshot.body.blocks.slice(0, 3), null, 2)}</pre>
      <button type="button" className="btn btn-sm" disabled={readonly} onClick={() => restore(item.revision)}>恢复此版本</button>
    </article>)}
    <div className="btn-row"><button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>较新版本</button>
      <span>第 {page} 页 · 共 {total} 个版本</span><button type="button" className="btn btn-sm" disabled={page * 30 >= total} onClick={() => setPage(page + 1)}>较早版本</button></div>
  </aside>;
}
