"""Actual Codex CLI + real HTTP transport against a new synthetic workspace.

Uses per-invocation MCP config and an environment token. Never edits global config.
The output is an acceptance record, not a production deployment or real-IdP test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time

import httpx
from docx import Document

from litworkbench import evidence, notes
from litworkbench.cloud_auth import CloudAuth, SESSION_COOKIE
from litworkbench.ingest import import_document
from litworkbench.runtime import RuntimeSettings
from litworkbench.store import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codex', default=shutil.which('codex'))
    args = parser.parse_args()
    root = args.output.resolve()
    repo = Path(__file__).resolve().parents[1]
    if root.exists() or root == repo or repo in root.parents:
        parser.error('--output must be a new directory outside the repository')
    if not args.codex: parser.error('Codex CLI not found')
    root.mkdir(parents=True)
    ws = Workspace(root / 'library'); ws.initialize()
    doc = Document(); doc.add_paragraph('Controlled source: museums preserve local inscriptions. 此为合成文献。😀')
    source = root / 'fixture.docx'; doc.save(source)
    imported = import_document(ws, source, ocr_mode='off')
    unit = ws.units_for_doc(imported['id'])[0]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    env = dict(os.environ)
    env.update(LIT_MODE='cloud', LIT_PUBLIC_URL=f'https://127.0.0.1:{port}', LIT_ALLOWED_HOSTS=f'127.0.0.1:{port}',
        LIT_OIDC_ISSUER='https://identity.example.test', LIT_OIDC_CLIENT_ID='fixture', LIT_OIDC_CLIENT_SECRET=secrets.token_urlsafe(32),
        LIT_OWNER_SUBJECT='fixture-owner', LIT_SESSION_SECRET=secrets.token_urlsafe(48), LIT_TOKEN_SECRET=secrets.token_urlsafe(48), LIT_WORKER_ENABLED='false')
    settings = RuntimeSettings.from_env(env); auth = CloudAuth(ws, settings)
    token = auth.issue_pat('acceptance-edit', 'edit')
    env['LITWORKBENCH_TEST_TOKEN'] = token['token']
    server_code = "import sys,uvicorn;from litworkbench.web.app import create_app;uvicorn.run(create_app(sys.argv[1],start_worker=False),host='127.0.0.1',port=int(sys.argv[2]),access_log=False,log_level='error')"
    server = subprocess.Popen([sys.executable, '-c', server_code, str(ws.root), str(port)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    result = {'environment': 'synthetic-loopback', 'production_oidc_tested': False, 'second_device_tested': False}
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(base + '/health', timeout=1).status_code == 200: break
            except httpx.HTTPError: pass
            if time.monotonic() > deadline: raise RuntimeError('Acceptance server did not start')
            time.sleep(.1)
        prompt = f'''Use ONLY the lit_fixture MCP tools; do not use shell, files, web or any other tools. This is an authorized synthetic acceptance test.
Read unit {unit['id']} using unit_read, reconstruct its json-fragment text, then create pending evidence for the exact quote "museums preserve local inscriptions" using its exact start_offset, version and text_sha256. Use request_id acceptance-evidence.
Create a page titled Codex acceptance with request_id acceptance-page. Read it, then use page_edit_blocks to insert one paragraph containing the exact text "Codex wrote this synthetic note. 中文😀". Use block id acceptance-block, parent_block_id null, index 0, request_id acceptance-blocks and the current expected_revision. Read back the page and its history. Return the page ID, evidence ID and revision. Do not include any credentials.'''
        command = [args.codex, 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
            '--sandbox', 'read-only', '-C', str(root),
            '-c', 'mcp_servers.lit_fixture.url=' + json.dumps(base + '/mcp'),
            '-c', 'mcp_servers.lit_fixture.bearer_token_env_var="LITWORKBENCH_TEST_TOKEN"',
            '-c', 'mcp_servers.lit_fixture.default_tools_approval_mode="approve"',
            '-c', 'mcp_servers.lit_fixture.enabled_tools=["unit_read","evidence_create","page_create","page_get","page_edit_blocks","page_get_history"]',
            '--json', prompt]
        run = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        transcript = (run.stdout + '\n' + run.stderr).replace(token['token'], '[REDACTED]')
        (root / 'codex-transcript.log').write_text(transcript, encoding='utf-8')
        result['codex_exit'] = run.returncode
        pages = notes.list_pages(ws)['items']; found = next((p for p in pages if p['title'] == 'Codex acceptance'), None)
        excerpts = evidence.list_evidence(ws)
        result['codex_content_roundtrip'] = bool(found and found['revision'] >= 2 and 'Codex wrote this synthetic note.' in str(found['body'])
            and len(excerpts) == 1 and excerpts[0]['review_status'] == 'pending'
            and notes.get_page_history(ws, found['id'])['items'][0]['origin'] == 'codex')
        if found: result['page_id'] = found['id']
        # TestClient is not used here: every operation below crosses the actual socket.
        def rpc(pat, method, params):
            return httpx.post(base + '/mcp', headers={'Authorization': 'Bearer ' + pat,
                'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, timeout=20)
        read = auth.issue_pat('acceptance-read', 'read')
        denied = rpc(read['token'], 'tools/call', {'name': 'page_create', 'arguments': {'title': 'Denied', 'request_id': 'denied'}})
        result['readonly_write_denied'] = denied.status_code == 200 and denied.json()['result']['isError']
        if found:
            original = next(item['snapshot'] for item in notes.get_page_history(ws, found['id'])['items'] if item['revision'] == 1)
            replay = rpc(token['token'], 'tools/call', {'name': 'page_create', 'arguments': {'title': 'Codex acceptance', 'request_id': 'acceptance-page'}})
            result['create_replay_once'] = not replay.json()['result'].get('isError') and notes.list_pages(ws)['total'] == 1
            browser = auth.issue_session()
            with httpx.Client(base_url=base, cookies={SESSION_COOKIE: browser.token}) as web:
                latest = web.get('/api/v1/pages/' + found['id']).json()
                saved = web.patch('/api/v1/pages/' + found['id'], headers={'X-CSRF-Token': browser.csrf_token, 'Origin': settings.public_url},
                    json={'title': 'Browser interleave', 'expected_revision': latest['revision'], 'request_id': 'browser-interleave'})
                conflict = rpc(token['token'], 'tools/call', {'name': 'page_update_properties', 'arguments': {
                    'page_id': found['id'], 'expected_revision': latest['revision'], 'request_id': 'agent-stale', 'properties': {'title': 'Stale agent'}}})
                result['web_agent_conflict'] = saved.status_code == 200 and conflict.json()['result']['isError']
        auth.revoke_pat(token['id'])
        result['revocation_denied'] = rpc(token['token'], 'tools/list', {}).status_code == 401
        result['accepted'] = run.returncode == 0 and all(result.get(k) is True for k in ['codex_content_roundtrip', 'readonly_write_denied', 'create_replay_once', 'web_agent_conflict', 'revocation_denied'])
    except (RuntimeError, subprocess.TimeoutExpired, httpx.HTTPError) as exc:
        result.update(accepted=False, error=type(exc).__name__)
    finally:
        server.terminate()
        try: server.wait(timeout=15)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
        (root / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('accepted') else 1


if __name__ == '__main__':
    raise SystemExit(main())
