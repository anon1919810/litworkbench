import type { DocumentItem } from '../api/types';
import type { NoteChanges, NotePage } from './types';
import { STATUSES } from './types';

export function PageProperties({ page, pages, documents, patch }: {
  page: NotePage; pages: NotePage[]; documents: DocumentItem[]; patch: (changes: NoteChanges) => void;
}) {
  const parentAllowed = (candidate: NotePage) => {
    const seen = new Set<string>(); let current: NotePage | undefined = candidate;
    while (current && !seen.has(current.id)) {
      if (current.id === page.id) return false;
      seen.add(current.id); current = pages.find((item) => item.id === current!.parent_id);
    }
    return !candidate.effective_archived;
  };
  return <details className="note-properties" open>
    <summary>页面属性</summary>
    <div className="note-property-grid">
      <label>状态<select value={page.status} disabled={page.effective_archived}
        onChange={(event) => patch({ status: event.target.value as NotePage['status'] })}>
        {Object.entries(STATUSES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>标签<input key={`${page.id}-${page.tags.join(',')}`} defaultValue={page.tags.join('，')}
        placeholder="用逗号分隔标签" disabled={page.effective_archived}
        onBlur={(event) => patch({ tags: event.target.value.split(/[,，；;]/).map((value) => value.trim()).filter(Boolean) })} /></label>
      <label>所属页面<select value={page.parent_id || ''} onChange={(event) => patch({ parent_id: event.target.value || null })}>
        <option value="">工作区根目录</option>
        {pages.filter(parentAllowed).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
      </select></label>
      <label>关联文献<select multiple value={page.document_ids} disabled={page.effective_archived}
        onChange={(event) => patch({ document_ids: [...event.target.selectedOptions].map((option) => option.value) })}>
        {documents.map((item) => <option key={item.id} value={item.id}>{item.title || item.filename}</option>)}
      </select><small>按住 Ctrl 可选择多份文献</small></label>
    </div>
  </details>;
}
