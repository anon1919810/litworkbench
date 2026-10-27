export interface InlineText { type: 'text'; text: string; styles: Record<string, unknown> }
export interface InlineLink { type: 'link'; href: string; content: InlineText[] }
export interface NoteBlock {
  id: string; type: string; props: Record<string, unknown>;
  content: (InlineText | InlineLink)[]; children: NoteBlock[];
}
export interface NoteBody { schema_version: number; blocks: NoteBlock[] }
export interface NotePage {
  id: string; parent_id: string | null; title: string; icon: string; body: NoteBody;
  revision: number; schema_version: number; tags: string[];
  status: 'not_started' | 'in_progress' | 'done'; favorite: boolean; position: number;
  archived: boolean; effective_archived: boolean; document_ids: string[];
  created_at: string; updated_at: string; url: string;
}
export type NoteChanges = Partial<Pick<NotePage, 'title' | 'icon' | 'body' | 'tags' | 'status' | 'favorite' | 'position' | 'archived' | 'parent_id' | 'document_ids'>>;
export type NoteWrite = NoteChanges & { expected_revision: number; request_id: string };
export interface NoteHistory { revision: number; origin: string; actor: string; created_at: string; snapshot: NotePage }
export interface Backlink { page_id: string; title: string; url: string; archived: boolean; effective_archived?: boolean }
export interface Asset { id: string; url: string; filename: string; media_type: string; sha256: string; byte_size: number }
export const EMPTY_BODY: NoteBody = { schema_version: 1, blocks: [] };
export const STATUSES = { not_started: '未开始', in_progress: '进行中', done: '已完成' } as const;
