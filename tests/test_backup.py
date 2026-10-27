import json

import pytest

from litworkbench.backup import backup_workspace, restore_workspace
from litworkbench.cloud_auth import CloudAuth
from litworkbench.errors import LitError
from litworkbench.ingest import import_document
from litworkbench.jobs import JobStore
from litworkbench.runtime import RuntimeSettings
from litworkbench import notes
from tests.test_runtime import cloud_env


def test_real_snapshot_restore_preserves_content_and_pauses_work(workspace, fixtures, tmp_path):
    doc = import_document(workspace, fixtures['text_pdf'], ocr_mode='off')
    page = notes.create_page(workspace, title='备份', parent_id=None, body=None, properties=None, request_id='backup-page')
    jobs = JobStore(workspace); job = jobs.enqueue_reprocess(doc['id'], [u['id'] for u in workspace.units_for_doc(doc['id'])])
    auth = CloudAuth(workspace, RuntimeSettings.from_env(cloud_env()))
    session = auth.issue_session(); token = auth.issue_pat('backup-test', 'edit')
    output = tmp_path / 'snapshot'
    manifest = backup_workspace(workspace, output)
    assert session.token not in str(manifest) and token['token'] not in str(manifest)
    restored = tmp_path / 'restored'
    report = restore_workspace(output, restored)
    from litworkbench.store import Workspace
    copy = Workspace(restored)
    assert notes.get_page(copy, page['id'])['title'] == '备份'
    assert copy.path_of(copy.get_document(doc['id'])).read_bytes() == workspace.path_of(doc).read_bytes()
    assert JobStore(copy).get(job['id'])['state'] == 'paused'
    assert report['paused_jobs'][0]['id'] == job['id']
    assert CloudAuth(copy, RuntimeSettings.from_env(cloud_env())).get_session(session.token) is None
    assert CloudAuth(copy, RuntimeSettings.from_env(cloud_env())).get_pat(token['token']) is None
    assert auth.get_session(session.token) is not None


def test_backup_missing_file_and_restore_tampering_fail(workspace, fixtures, tmp_path):
    doc = import_document(workspace, fixtures['text_pdf'], ocr_mode='off')
    output = tmp_path / 'snapshot'
    manifest = backup_workspace(workspace, output)
    source = next(item for item in manifest['files'] if item['path'].startswith('sources/'))
    (output / source['path']).write_bytes(b'tampered')
    with pytest.raises(LitError): restore_workspace(output, tmp_path / 'bad-restore')
    assert not (tmp_path / 'bad-restore').exists()
    workspace.path_of(doc).unlink()
    with pytest.raises(LitError): backup_workspace(workspace, tmp_path / 'missing')


def test_backup_target_and_manifest_paths_are_bounded(workspace, tmp_path):
    with pytest.raises(LitError): backup_workspace(workspace, workspace.root / 'backup')
    output = tmp_path / 'snapshot'; backup_workspace(workspace, output)
    with pytest.raises(LitError): backup_workspace(workspace, output)
    manifest_path = output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    manifest['files'][0]['path'] = '../escape'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(LitError): restore_workspace(output, tmp_path / 'escape-restore')
