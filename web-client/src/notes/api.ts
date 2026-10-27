import { apiGet, apiPatch, apiPost } from '../api/client';
import type { Page } from '../api/types';
import type { Asset, Backlink, NoteBody, NoteHistory, NotePage, NoteWrite } from './types';

const id = encodeURIComponent;
export const getPage = (value: string) => apiGet<NotePage>(`/pages/${id(value)}`);
export const listPages = (query = '', archived = false, page = 1) => apiGet<Page<NotePage>>(
  `/pages?q=${id(query)}&include_archived=${archived}&page_size=100&page=${page}`);
export const savePage = (value: string, patch: NoteWrite) => apiPatch<NotePage>(`/pages/${id(value)}`, patch);
export const createPage = (title: string, parent_id: string | null = null, body?: NoteBody) => apiPost<NotePage>('/pages', {
  title, parent_id, body, request_id: crypto.randomUUID(),
});
export const history = (value: string, page = 1) => apiGet<Page<NoteHistory>>(`/pages/${id(value)}/history?page=${page}`);
export const backlinks = (value: string) => apiGet<Page<Backlink>>(`/pages/${id(value)}/backlinks`);
export const restorePage = (page: NotePage, revision: number) => apiPost<NotePage>(`/pages/${id(page.id)}/restore`, {
  revision, expected_revision: page.revision, request_id: crypto.randomUUID(),
});
export const uploadImage = (file: File) => { const form = new FormData(); form.append('file', file);
  return apiPost<Asset>('/note-assets', form); };
export const exportPage = (value: string, format: 'json' | 'markdown' | 'zip') => apiPost<{ download_url: string; url?: string; id: string }>(
  `/pages/${id(value)}/exports`, { format });
