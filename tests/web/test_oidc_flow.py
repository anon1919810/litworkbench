"""Real Authlib signature/claims/state/nonce/PKCE checks against a controlled IdP."""
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from litworkbench.cloud_auth import SESSION_COOKIE
from tests.web.test_cloud_auth import cloud_app


@pytest.fixture
def oidc(cloud_app):
    key = RSAKey.generate_key(2048, parameters={'kid': 'fixture'})
    state = {'attack': '', 'nonce': '', 'pkce': '', 'token_calls': 0}
    issuer = cloud_app.state.settings.oidc_issuer

    def provider(request):
        if request.url.path.endswith('openid-configuration'):
            return httpx.Response(200, json={'issuer': issuer, 'authorization_endpoint': issuer + '/authorize',
                'token_endpoint': issuer + '/token', 'jwks_uri': issuer + '/jwks',
                'id_token_signing_alg_values_supported': ['RS256']})
        if request.url.path == '/jwks':
            return httpx.Response(200, json={'keys': [key.as_dict(private=False)]})
        assert request.url.path == '/token'
        state['token_calls'] += 1
        form = parse_qs(request.content.decode())
        verifier = form['code_verifier'][0]
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=') == state['pkce']
        claims = {'iss': issuer, 'sub': 'owner-sub', 'aud': 'fixture', 'nonce': state['nonce'],
                  'exp': int(time.time()) + 300, 'iat': int(time.time())}
        attack = state['attack']
        if attack in ('iss', 'sub', 'aud', 'nonce'):
            claims[attack] = 'wrong'
        if attack == 'expired': claims['exp'] = int(time.time()) - 600
        token = jwt.encode({'alg': 'RS256', 'kid': 'fixture'}, claims, key)
        if attack == 'signature':
            token = token.rsplit('.', 1)[0] + '.invalid'
        result = {'access_token': 'fixture-access', 'token_type': 'Bearer', 'id_token': token}
        if attack == 'missing': result.pop('id_token')
        return httpx.Response(200, json=result)

    cloud_app.state.oidc.client_kwargs['transport'] = httpx.MockTransport(provider)
    return cloud_app, state


def begin(client, state, return_to='/pages/pg_example'):
    response = client.get('/auth/login', params={'return_to': return_to}, follow_redirects=False)
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['code_challenge_method'] == ['S256']
    state['nonce'], state['pkce'] = query['nonce'][0], query['code_challenge'][0]
    return query['state'][0]


def test_valid_login_secure_cookie_redirect_and_replay(oidc):
    app, state = oidc
    with TestClient(app, base_url='https://lit.example.test') as client:
        challenge = begin(client, state)
        response = client.get('/auth/callback', params={'state': challenge, 'code': 'fixture-code'}, follow_redirects=False)
        assert response.status_code == 303, response.text
        assert response.headers['location'] == '/pages/pg_example'
        cookie = response.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
        assert client.get('/api/v1/workspace').status_code == 200
        client.cookies.delete(SESSION_COOKIE)
        assert client.get('/auth/callback', params={'state': challenge, 'code': 'fixture-code'}).status_code == 400
        assert state['token_calls'] == 1


@pytest.mark.parametrize('attack', ['iss', 'sub', 'aud', 'nonce', 'expired', 'signature', 'missing', 'state'])
def test_invalid_login_never_grants_a_session(oidc, attack):
    app, state = oidc
    with TestClient(app, base_url='https://lit.example.test') as client:
        challenge = begin(client, state)
        state['attack'] = attack
        response = client.get('/auth/callback', params={'state': 'wrong' if attack == 'state' else challenge, 'code': 'fixture-code'}, follow_redirects=False)
        assert response.status_code == 400
        assert SESSION_COOKIE not in client.cookies
        assert client.get('/api/v1/workspace').status_code == 401


def test_external_return_url_is_rejected(oidc):
    app, state = oidc
    with TestClient(app, base_url='https://lit.example.test') as client:
        challenge = begin(client, state, '//evil.test')
        response = client.get('/auth/callback', params={'state': challenge, 'code': 'fixture-code'}, follow_redirects=False)
        assert response.headers['location'] == '/pages'
