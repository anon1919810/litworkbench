import json

import pytest

from litworkbench.cloud_auth import Principal
from litworkbench.errors import LitError
from litworkbench.mcp_paging import paginate
from litworkbench.mcp_tools import ContentTools
from litworkbench import notes
from tests.test_notes import body, text_block

EDIT = Principal('owner', 'pat-fixture', 'codex', frozenset({'read', 'edit'}))
READ = Principal('owner', 'pat-read', 'codex', frozenset({'read'}))


def test_paging_roundtrip_bound_tamper_and_revision():
    data = {'body': ('中文😀\\\n"' * 8000)}
    chunks, cursor = [], None
    while True:
        result = paginate(data, key='k' * 40, identity='page:one', revision=3, cursor=cursor, max_chars=2048)
        assert len(json.dumps(result, ensure_ascii=False)) <= 2048
        chunks.append(result['text']); cursor = result['next_cursor']
        if not result['truncated']: break
    assert json.loads(''.join(chunks)) == data
    token = paginate(data, key='k' * 40, identity='page:one', revision=3, max_chars=2048)['next_cursor']
    with pytest.raises(LitError): paginate(data, key='k' * 40, identity='page:other', revision=3, cursor=token)
    with pytest.raises(LitError): paginate(data, key='k' * 40, identity='page:one', revision=4, cursor=token)
    with pytest.raises(LitError): paginate(data, key='k' * 40, identity='page:one', revision=3, cursor=token + 'x')


def test_tools_share_revision_idempotency_and_codex_history(workspace):
    tools = ContentTools(workspace)
    page = tools.create(EDIT, 'Agent page', None, None, None, 'agent-create')
    assert tools.create(EDIT, 'Agent page', None, None, None, 'agent-create')['id'] == page['id']
    changed = tools.update(EDIT, page['id'], page['revision'], 'agent-update', {'tags': ['研究']})
    assert changed['revision'] == 2
    with pytest.raises(LitError): tools.update(EDIT, page['id'], 1, 'stale', {'title': 'old'})
    assert notes.get_page_history(workspace, page['id'])['items'][0]['origin'] == 'codex'
    with pytest.raises(PermissionError): tools.create(READ, 'No', None, None, None, 'denied')
    with pytest.raises(LitError): tools.update(EDIT, page['id'], 2, 'archive', {'archived': True})
    with pytest.raises(LitError): tools.edit(EDIT, page['id'], 2, 'too-many', [{'op': 'delete'}] * 101)


def test_evidence_strict_idempotent_and_pending(workspace, fixtures):
    from litworkbench.ingest import import_document
    from litworkbench.evidence import effective_text
    from litworkbench.store import sha256_text
    imported = import_document(workspace, fixtures['text_pdf'], ocr_mode='off')
    unit = workspace.units_for_doc(imported['id'])[0]
    text, version = effective_text(unit)
    tools = ContentTools(workspace)
    args = (EDIT, unit['id'], text[:8], 0, sha256_text(text), version, '', '', 'evidence-req')
    saved = tools.evidence_create(*args)
    assert saved['review_status'] == 'pending'
    assert tools.evidence_create(*args)['id'] == saved['id']
    with pytest.raises(LitError):
        tools.evidence_create(EDIT, unit['id'], 'invented', 0, sha256_text(text), version, '', '', 'wrong')
    with workspace.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1
