"""Transport-independent content operations. Documents are data, never authority."""
from __future__ import annotations

import json

from litworkbench import evidence, notes
from litworkbench.errors import ValidationError
from litworkbench.store import Workspace, sha256_text

PROPERTY_KEYS = {'title', 'icon', 'tags', 'status', 'favorite', 'document_ids'}


class ContentTools:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    @staticmethod
    def require(principal, capability='read'):
        if principal is None or capability not in principal.capabilities or principal.origin != 'codex':
            raise PermissionError('令牌没有此操作的权限。')

    @staticmethod
    def receipt(page):
        return {key: page[key] for key in ('id', 'revision', 'url')}

    def create(self, principal, title, parent_id, body, properties, request_id):
        self.require(principal, 'edit')
        if properties and set(properties) - (PROPERTY_KEYS - {'title'}):
            raise ValidationError('Codex 只能设置固定页面属性。')
        return self.receipt(notes.create_page(self.workspace, title=title, parent_id=parent_id,
            body=body, properties=properties, request_id=request_id, actor='owner', origin='codex'))

    def update(self, principal, page_id, expected_revision, request_id, properties):
        self.require(principal, 'edit')
        if not isinstance(properties, dict) or set(properties) - PROPERTY_KEYS:
            raise ValidationError('Codex 只能修改标题、图标、标签、状态、收藏或关联文献。')
        return self.receipt(notes.update_page(self.workspace, page_id, expected_revision=expected_revision,
            request_id=request_id, changes=properties, actor='owner', origin='codex'))

    def edit(self, principal, page_id, expected_revision, request_id, operations):
        self.require(principal, 'edit')
        return self.receipt(notes.edit_page_blocks(self.workspace, page_id, expected_revision=expected_revision,
            request_id=request_id, operations=operations, actor='owner', origin='codex'))

    def document(self, principal, document_id):
        self.require(principal)
        with self.workspace.connect() as conn:
            conn.execute('BEGIN')
            doc = self.workspace.get_document(document_id, connection=conn)
            for key in ('original_path', 'stored_path'): doc.pop(key, None)
            units = conn.execute('SELECT id,seq,kind,file_page,printed_page,printed_page_status,status,method FROM units WHERE doc_id=? ORDER BY seq', (document_id,)).fetchall()
            doc['units'] = [dict(row) | {'url': '/?unit=' + row['id']} for row in units]
        return doc | {'url': '/?doc=' + document_id}

    def unit(self, principal, unit_id):
        self.require(principal)
        unit = self.workspace.get_unit(unit_id)
        text, version = evidence.effective_text(unit)
        return {key: unit.get(key) for key in ('id', 'doc_id', 'kind', 'seq', 'file_page', 'printed_page',
            'printed_page_status', 'method', 'status')} | {
            'text': text, 'version': version, 'text_sha256': sha256_text(text), 'url': '/?unit=' + unit_id}

    def search(self, principal, query, scope='all', page=1):
        self.require(principal)
        if scope not in {'all', 'pages', 'documents', 'evidence'} or type(page) is not int or page < 1:
            raise ValidationError('无效的检索范围或页码。')
        if not isinstance(query, str) or len(query) > 500:
            raise ValidationError('检索词最多 500 个字符。')
        result = {}
        if scope in {'all', 'pages'}:
            pages = notes.list_pages(self.workspace, query=query, page=page, page_size=30)
            result['pages'] = pages | {'items': [{k: p[k] for k in ('id', 'title', 'revision', 'url')} for p in pages['items']]}
        with self.workspace.connect() as conn:
            conn.execute('BEGIN')
            for kind, table, fields in [('documents', 'documents', "COALESCE(title,'') || ' ' || filename || ' ' || COALESCE(author,'')"),
                                        ('evidence', 'evidence', "quote || ' ' || note || ' ' || category")]:
                if scope not in {'all', kind}: continue
                where, arg = fields + ' LIKE ? ESCAPE \'!\'', '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
                count = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', (arg,)).fetchone()[0]
                cols = 'id,title,filename,updated_at' if kind == 'documents' else 'id,quote,unit_id,review_status,updated_at'
                rows = conn.execute(f'SELECT {cols} FROM {table} WHERE {where} ORDER BY updated_at DESC,id LIMIT 30 OFFSET ?', (arg, (page - 1) * 30)).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    if 'quote' in item: item['quote'] = item['quote'][:300]
                    item['url'] = '/?doc=' + row['id'] if kind == 'documents' else '/?unit=' + row['unit_id']
                    items.append(item)
                result[kind] = {'items': items, 'page': page, 'page_size': 30, 'total': count}
        return result

    def evidence_create(self, principal, unit_id, quote, start_offset, expected_hash, version, category, note, request_id):
        self.require(principal, 'edit')
        notes._require_request_id(request_id)
        if (not isinstance(expected_hash, str) or len(expected_hash) != 64 or not isinstance(quote, str)
                or not quote or len(quote) > 20000 or len(note) > 20000 or len(category) > 200):
            raise ValidationError('摘录需要文字哈希和非空逐字引文，文字长度须在限制内。')
        payload = dict(unit_id=unit_id, quote=quote, start_offset=start_offset, expected_hash=expected_hash,
                       version=version, category=category, note=note)
        digest = notes._digest('evidence_create', payload)
        with self.workspace.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = notes._check_mutation_request(conn, actor='owner', request_id=request_id, operation='evidence_create', digest=digest)
            if previous: return previous
            saved = evidence.save_evidence(self.workspace, unit_id, quote, category=category, note=note,
                start_offset=start_offset, expected_hash=expected_hash, version=version, strict=True, connection=conn)
            snapshot = saved['version_snapshot'] | {'origin': 'codex', 'actor': principal.actor_id}
            conn.execute('UPDATE evidence SET version_snapshot=? WHERE id=?', (json.dumps(snapshot, ensure_ascii=False), saved['id']))
            result = {'id': saved['id'], 'review_status': saved['review_status'], 'url': '/?unit=' + unit_id}
            notes._record_mutation_request(conn, actor='owner', request_id=request_id, operation='evidence_create', digest=digest, result=result)
        return result
