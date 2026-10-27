import { useEffect, useState } from 'react';
import { listDocuments, listEvidence } from '../api/endpoints';
import type { NoteBlock, NotePage } from './types';

type Option = { id: string; label: string };
export function PageReferences({ pages, insert, close }: {
  pages: NotePage[]; insert: (block: Partial<NoteBlock>) => void; close: () => void;
}) {
  const [kind, setKind] = useState('pageRef'); const [page, setPage] = useState(1);
  const [options, setOptions] = useState<Option[]>([]); const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState(''); const [error, setError] = useState(''); const [filter, setFilter] = useState('');
  useEffect(() => {
    let alive = true; setSelected(''); setError('');
    if (kind === 'pageRef') { setOptions(pages.map((item) => ({ id: item.id, label: item.title }))); setTotal(pages.length); return; }
    const query = kind === 'documentRef' ? listDocuments(page, 30).then((result) => ({ total: result.total,
      items: result.items.map((item) => ({ id: item.id, label: item.title || item.filename })) })) :
      listEvidence({ page, pageSize: 30 }).then((result) => ({ total: result.total,
        items: result.items.map((item) => ({ id: item.id, label: `${item.quote.slice(0, 90)} · ${item.review_status}` })) }));
    void query.then((result) => { if (alive) { setOptions(result.items); setTotal(result.total); } })
      .catch((cause: Error) => { if (alive) setError(cause.message); });
    return () => { alive = false; };
  }, [kind, page, pages]);
  return <section className="note-reference-picker" aria-label="插入关联引用">
    <div className="note-panel-heading"><strong>插入关联引用</strong><button type="button" className="btn btn-sm" onClick={close}>关闭</button></div>
    <label>引用类型<select value={kind} onChange={(event) => { setKind(event.target.value); setPage(1); }}>
      <option value="pageRef">笔记页面</option><option value="documentRef">文献</option><option value="evidenceRef">原文摘录</option>
    </select></label>
    <input aria-label="筛选当前页引用" placeholder="筛选当前页内容" value={filter} onChange={(event) => setFilter(event.target.value)} />
    <select aria-label="选择引用" size={6} value={selected} onChange={(event) => setSelected(event.target.value)}>
      {options.filter((item) => item.label.includes(filter)).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select>
    {error ? <p role="alert">{error}</p> : null}
    {kind !== 'pageRef' ? <div className="btn-row"><button type="button" className="btn btn-sm" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button>
      <span>第 {page} 页 / 共 {total} 条</span><button type="button" className="btn btn-sm" disabled={page * 30 >= total} onClick={() => setPage(page + 1)}>下一页</button></div> : null}
    <button type="button" className="btn btn-primary" disabled={!selected} onClick={() => {
      const key = kind === 'pageRef' ? 'pageId' : kind === 'documentRef' ? 'documentId' : 'evidenceId';
      insert({ id: crypto.randomUUID(), type: kind, props: { [key]: selected }, content: [], children: [] }); close();
    }}>插入到正文</button>
  </section>;
}
