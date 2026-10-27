import pytest
from fastapi.testclient import TestClient

from litworkbench.cloud_auth import SESSION_COOKIE
from litworkbench.runtime import RuntimeSettings
from litworkbench.web.app import create_app
from tests.test_runtime import cloud_env


@pytest.fixture
def cloud_app(web_workspace):
    return create_app(web_workspace.root, start_worker=False, settings=RuntimeSettings.from_env(cloud_env()))


@pytest.mark.parametrize('path', [
    '/api/v1/workspace', '/api/v1/documents', '/api/v1/pages', '/api/v1/jobs',
    '/api/v1/evidence', '/api/v1/documents/unknown/source', '/api/v1/units/unknown/render',
    '/api/v1/note-assets/unknown', '/api/v1/page-exports/unknown/download',
    '/api/v1/exports/unknown/download', '/api/v1/agent-tokens',
])
def test_every_content_entry_requires_login(cloud_app, path):
    with TestClient(cloud_app, base_url='https://lit.example.test') as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'LOGIN_REQUIRED'
        assert 'Set-Cookie' not in response.headers


def test_host_checks_and_cloud_bootstrap(cloud_app):
    with TestClient(cloud_app, base_url='https://lit.example.test') as client:
        assert client.get('/health').json() == {'status': 'ok'}
        assert client.get('/health', headers={'Host': 'evil.test', 'X-Forwarded-Host': 'lit.example.test'}).status_code == 403
        session = cloud_app.state.cloud_auth.issue_session()
        client.cookies.set(SESSION_COOKIE, session.token)
        info = client.get('/api/v1/workspace')
        assert info.status_code == 200
        assert 'root' not in info.json()
        assert info.json()['mode'] == 'cloud'
        assert info.json()['csrf_token'] == session.csrf_token
        assert client.post('/api/v1/agent-tokens', json={'name': 'test', 'scope': 'read'}).status_code == 403
        headers = {'X-CSRF-Token': session.csrf_token, 'Origin': 'https://evil.test'}
        assert client.post('/api/v1/agent-tokens', headers=headers, json={'name': 'test', 'scope': 'read'}).status_code == 403
        headers['Origin'] = 'https://lit.example.test'
        token = client.post('/api/v1/agent-tokens', headers=headers, json={'name': 'test', 'scope': 'read'})
        assert token.status_code == 201, token.text
        assert 'token' not in str(client.get('/api/v1/agent-tokens').json()['items'][0].keys())
        logout = client.post('/auth/logout', headers=headers)
        assert logout.status_code == 200
        assert client.get('/api/v1/pages').status_code == 401
        assert client.get('/api/v1/pages', headers={'Authorization': 'Bearer ' + token.json()['token']}).status_code == 401


def test_cloud_does_not_initialize_an_accidental_empty_volume(tmp_path):
    with pytest.raises(ValueError, match='initialized'):
        create_app(tmp_path / 'empty', settings=RuntimeSettings.from_env(cloud_env()), start_worker=False)
    assert not (tmp_path / 'empty').exists()


def test_reserved_routes_never_return_spa(cloud_app):
    with TestClient(cloud_app, base_url='https://lit.example.test') as client:
        for path in ['/auth/missing', '/.well-known/missing']:
            response = client.get(path)
            assert response.status_code == 404
            assert 'text/html' not in response.headers.get('content-type', '')
        assert client.get('/auth/callback?code=forged&state=forged').status_code == 400
