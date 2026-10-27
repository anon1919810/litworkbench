import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { listDocuments } from '../api/endpoints';
import type { DocumentItem } from '../api/types';
import { backlinks, createPage, exportPage, getPage, listPages, restorePage, uploadImage } from './api';
import { PageHistory } from './PageHistory';
import { PageProperties } from './PageProperties';
import { PageReferences } from './PageReferences';
import { usePageEditor } from './usePageEditor';
import type { Backlink, NoteBlock, NotePage } from './types';
import { STATUSES } from './types';
import { pageTree } from './tree';
import './notes.css';

const PageEditor = lazy(() => import('./PageEditor'));
const selectedFromUrl = () => { const value = location.pathname.match(/^\/pages\/([^/]+)/); return value ? decodeURIComponent(value[1]) : null; };
const saveLabels = { saved: '已保存', dirty: '未保存', saving: '保存中…', error: '保存失败 · 草稿已保留', conflict: '有冲突 · 草稿已保留' };

export default function NotesPage() {
  const [selected, setSelected] = useState<string | null>(selectedFromUrl);
  const [pages, setPages] = useState<NotePage[]>([]); const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [query, setQuery] = useState(''); const [status, setStatus] = useState(''); const [tag, setTag] = useState('');
  const [showArchived, setShowArchived] = useState(false); const [table, setTable] = useState(false);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [showHistory, setShowHistory] = useState(false);
  const [showReferences, setShowReferences] = useState(false); const [links, setLinks] = useState<Backlink[]>([]);
  const [serverVersion, setServerVersion] = useState<NotePage | null>(null);
  const insert = useRef<((block: Partial<NoteBlock>) => void) | null>(null);
  const refresh = useCallback(() => {
    let aborted = false;
    void (async () => { const all: NotePage[] = []; let number = 1;
      while (!aborted) { const result = await listPages(query, showArchived, number++); all.push(...result.items);
        if (all.length >= result.total || !result.items.length) break; }
      if (!aborted) setPages(all);
    })().catch((cause: Error) => { if (!aborted) setError(cause.message); });
    return () => { aborted = true; };
  }, [query, showArchived]);
  const { draft, error: loadError, epoch, reload } = usePageEditor(selected, refresh);
  const page = draft?.view;
  useEffect(refresh, [refresh]);
  useEffect(() => { let alive = true; void (async () => {
    const all: DocumentItem[] = []; let number = 1;
    while (alive) { const data = await listDocuments(number++, 100); all.push(...data.items); if (all.length >= data.total || !data.items.length) break; }
    if (alive) setDocuments(all);
  })().catch(() => {}); return () => { alive = false; }; }, []);
  useEffect(() => { const handle = () => { setSelected(selectedFromUrl()); setShowHistory(false); setShowReferences(false); };
    window.addEventListener('popstate', handle); return () => window.removeEventListener('popstate', handle); }, []);
  useEffect(() => {
    let alive = true; setLinks([]); setServerVersion(null);
    if (selected) void backlinks(selected).then((result) => { if (alive) setLinks(result.items); }).catch(() => {});
    return () => { alive = false; };
  }, [selected, page?.revision]);
  useEffect(() => {
    let alive = true;
    if (selected && draft?.status === 'conflict') void getPage(selected).then((result) => { if (alive) setServerVersion(result); }).catch(() => {});
    return () => { alive = false; };
  }, [selected, draft?.status]);
  const act = async (work: () => Promise<void>) => { setError(''); setBusy(true); try { await work(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); } finally { setBusy(false); } };
  const open = (id: string | null) => { history.pushState(null, '', id ? `/pages/${encodeURIComponent(id)}` : '/pages');
    setSelected(id); setShowHistory(false); setShowReferences(false); };
  const add = (parent: string | null = null) => void act(async () => {
    const created = await createPage('无标题', parent); refresh(); open(created.id);
  });
  const visible = pages.filter((item) => (!status || item.status === status) && (!tag || item.tags.includes(tag)));
  const siblings = page ? pages.filter((item) => item.parent_id === page.parent_id).sort((a, b) => a.position - b.position) : [];
  const siblingIndex = siblings.findIndex((item) => item.id === page?.id);
  return <div className="notes-workspace">
    <aside className="notes-browser">
      <div className="note-panel-heading"><h2>笔记工作区</h2><button className="btn btn-sm" type="button" disabled={busy} onClick={() => add()}>新建页面</button></div>
      <input aria-label="搜索笔记" placeholder="搜索页面、正文或标签" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div className="note-filters"><select aria-label="按状态筛选" value={status} onChange={(event) => setStatus(event.target.value)}>
        <option value="">全部状态</option>{Object.entries(STATUSES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
        <select aria-label="按标签筛选" value={tag} onChange={(event) => setTag(event.target.value)}><option value="">全部标签</option>
          {[...new Set(pages.flatMap((item) => item.tags))].map((value) => <option key={value}>{value}</option>)}</select></div>
      <label className="note-archive-filter"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />包含归档页面</label>
      <button type="button" className="note-overview-link" onClick={() => { open(null); setTable(true); }}>▦ 属性表格 · {visible.length} 页</button>
      <nav aria-label="笔记页面树">{pageTree(visible).map(({ item, depth }) => <button key={item.id} type="button" className={`note-tree-item ${selected === item.id ? 'active' : ''}`}
        style={{ paddingLeft: 10 + depth * 14 }} onClick={() => open(item.id)}>
        <span>{item.favorite ? '★' : item.icon || '▤'}</span><span>{item.title}</span>{item.effective_archived ? <small>归档</small> : null}
      </button>)}</nav>
      {!pages.length ? <p className="help">还没有页面。新建一页，开始整理材料和想法。</p> : null}
    </aside>
    <section className="notes-content">
      {(error || loadError) ? <div className="note-error" role="alert">{error || loadError}<button type="button" className="btn btn-sm" onClick={() => setError('')}>关闭提示</button></div> : null}
      {!selected ? <div className="notes-overview"><span className="note-eyebrow">个人研究工作区</span><h1>让材料与想法相遇。</h1>
        <p>把文献、原文摘录和自己的思考，组织成可以继续生长的页面。</p>
        <div className="btn-row"><button type="button" className="btn btn-primary" onClick={() => add()}>新建研究页面</button><button type="button" className="btn" onClick={() => setTable(!table)}>{table ? '收起表格' : '查看属性表格'}</button></div>
        {table || pages.length ? <div className="notes-table-wrap"><table className="notes-table"><thead><tr><th>页面</th><th>状态</th><th>标签</th><th>更新时间</th></tr></thead>
          <tbody>{visible.map((item) => <tr key={item.id}><td><button type="button" onClick={() => open(item.id)}>{item.icon || '▤'} {item.title}</button></td><td>{STATUSES[item.status]}</td><td>{item.tags.join(' · ')}</td><td>{new Date(item.updated_at).toLocaleString()}</td></tr>)}</tbody></table></div> : null}
      </div> : !page || !draft ? <p className="help note-loading">正在读取页面…</p> : <>
        <div className="note-toolbar"><a href="/pages" onClick={(event) => { event.preventDefault(); open(null); }}>工作区</a><span>/ {page.title}</span>
          <span className={`note-save-state ${draft.status}`} role="status">{saveLabels[draft.status]}</span>
          <button type="button" className="btn btn-sm" onClick={() => void draft.flush()} disabled={page.effective_archived || !draft.dirty}>保存</button>
          <button type="button" className="btn btn-sm" onClick={() => setShowHistory(!showHistory)}>历史</button>
          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void act(async () => { if (!await draft.flush()) throw new Error('请先处理未保存的草稿。'); const result = await exportPage(page.id, 'zip'); const link = document.createElement('a'); link.href = result.download_url; link.download = ''; link.click(); })}>导出</button>
        </div>
        <div className="note-page-layout"><article className="note-paper">
          {page.effective_archived ? <div className="note-archive-banner">此页面已归档，内容只读。<button type="button" className="btn btn-sm" onClick={() => draft.patch({ archived: false })}>取消本页归档</button>{!page.archived ? <small>父页面仍归档时，需要先恢复父页面或将本页移出。</small> : null}</div> : null}
          <div className="note-page-actions"><button type="button" className="note-icon" title="收藏页面" disabled={page.effective_archived} onClick={() => draft.patch({ favorite: !page.favorite })}>{page.favorite ? '★' : '☆'}</button>
            <input aria-label="页面图标" className="note-emoji-input" key={page.id} defaultValue={page.icon} placeholder="▤" disabled={page.effective_archived} onBlur={(event) => draft.patch({ icon: event.target.value })} />
            <button type="button" className="btn btn-sm" disabled={page.effective_archived || busy} onClick={() => add(page.id)}>新建子页面</button>
            <button type="button" className="btn btn-sm" disabled={page.effective_archived} onClick={() => draft.patch({ archived: true })}>归档</button>
          </div>
          <input className="note-title" aria-label="页面标题" value={page.title} disabled={page.effective_archived} onChange={(event) => draft.patch({ title: event.target.value })} />
          <PageProperties page={page} pages={pages} documents={documents} patch={(value) => draft.patch(value)} />
          <div className="btn-row"><button type="button" className="btn btn-sm" disabled={page.effective_archived || siblingIndex <= 0 || !!query} onClick={() => draft.patch({ position: siblingIndex - 1 })}>同级上移</button>
            <button type="button" className="btn btn-sm" disabled={page.effective_archived || siblingIndex < 0 || siblingIndex >= siblings.length - 1 || !!query} onClick={() => draft.patch({ position: siblingIndex + 1 })}>同级下移</button></div>
          {draft.error ? <div className="note-error" role="alert"><strong>{saveLabels[draft.status]}</strong><p>{draft.error.message}</p>
            {draft.status !== 'conflict' ? <button type="button" className="btn btn-sm" onClick={() => void draft.retry()}>重试原请求</button> : <>
              {serverVersion ? <details><summary>查看服务器版本 {serverVersion.revision}</summary><pre>{JSON.stringify({ title: serverVersion.title, body: serverVersion.body }, null, 2)}</pre></details> : null}
              <button type="button" className="btn btn-sm" onClick={() => void act(() => reload(true))}>将我的草稿应用到最新版本</button>
              <button type="button" className="btn btn-sm" onClick={() => { if (confirm('载入服务器版本会放弃当前未保存草稿。是否继续？')) void act(() => reload(false)); }}>放弃草稿，载入服务器版本</button>
            </>}
            <button type="button" className="btn btn-sm" onClick={() => void navigator.clipboard.writeText(JSON.stringify(page, null, 2))}>复制草稿</button>
          </div> : null}
          <div className="note-insert-toolbar"><button type="button" className="btn btn-sm" disabled={page.effective_archived} onClick={() => setShowReferences(!showReferences)}>＋ 关联引用</button>
            <label className="btn btn-sm">＋ 图片<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden disabled={page.effective_archived || busy} onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void act(async () => { const asset = await uploadImage(file); insert.current?.({ id: crypto.randomUUID(), type: 'image', props: { assetId: asset.id, src: asset.url, caption: asset.filename, width: 640 }, content: [], children: [] }); }); }} /></label>
            <small>输入 / 选择内容块</small></div>
          {showReferences ? <PageReferences pages={pages} insert={(block) => insert.current?.(block)} close={() => setShowReferences(false)} /> : null}
          <Suspense fallback={<p className="help">正在加载块编辑器…</p>}><PageEditor key={`${page.id}-${epoch}`} body={page.body} readonly={page.effective_archived}
            onChange={(body) => draft.patch({ body })} onComposition={(active) => { draft.composing = active; if (!active) draft.schedule(); }}
            onReady={(callback) => { insert.current = callback; }} /></Suspense>
          <footer className="note-backlinks"><h3>反向链接</h3>{links.length ? links.map((item, index) => <a key={`${item.page_id}-${index}`} href={item.url} onClick={(event) => { event.preventDefault(); open(item.page_id); }}>{item.title}{item.effective_archived ? '（归档）' : ''}</a>) : <p>还没有其他页面引用这里。</p>}</footer>
        </article>{showHistory ? <PageHistory id={page.id} readonly={page.effective_archived} close={() => setShowHistory(false)} restore={(revision) => void act(async () => { if (!await draft.flush()) throw new Error('请先保存或处理当前草稿。'); const latest = await getPage(page.id); await restorePage(latest, revision); await reload(false); refresh(); setShowHistory(false); })} /> : null}</div>
      </>}
    </section>
  </div>;
}
