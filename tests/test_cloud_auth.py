import pytest

from litworkbench.cloud_auth import CloudAuth
from litworkbench.runtime import RuntimeSettings
from tests.test_runtime import cloud_env


def test_session_survives_restart_but_is_revocable(workspace):
    settings = RuntimeSettings.from_env(cloud_env())
    auth = CloudAuth(workspace, settings)
    issued = auth.issue_session()
    assert CloudAuth(workspace, settings).get_session(issued.token).csrf_token == issued.csrf_token
    assert auth.get_session('invented') is None
    assert auth.get_pat(issued.token) is None
    auth.revoke_session(issued.token)
    assert auth.get_session(issued.token) is None


def test_pat_one_time_secret_scopes_expiry_and_revocation(workspace):
    auth = CloudAuth(workspace, RuntimeSettings.from_env(cloud_env()))
    result = auth.issue_pat('Codex', 'read', days=1)
    assert len(result['token']) >= 43
    principal = auth.get_pat(result['token'])
    assert 'read' in principal.capabilities and 'edit' not in principal.capabilities
    assert auth.get_session(result['token']) is None
    assert result['token'] not in str(auth.list_pats())
    with workspace.connect() as conn:
        raw = str([tuple(row) for row in conn.execute('SELECT * FROM agent_tokens')])
        assert result['token'] not in raw
    auth.revoke_pat(result['id'])
    assert auth.get_pat(result['token']) is None
    edit = auth.issue_pat('write', 'edit')
    assert 'edit' in auth.get_pat(edit['token']).capabilities
    with workspace.connect() as conn:
        conn.execute('UPDATE agent_tokens SET expires_at=0')
    assert auth.get_pat(edit['token']) is None


def test_oauth_flow_is_short_lived_and_consumed_once(workspace):
    auth = CloudAuth(workspace, RuntimeSettings.from_env(cloud_env()))
    token = auth.save_flow({'state': 's', 'nonce': 'n'})
    assert auth.take_flow(token) == {'state': 's', 'nonce': 'n'}
    assert auth.take_flow(token) is None
