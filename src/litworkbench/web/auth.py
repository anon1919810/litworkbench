"""OIDC transport and browser-only token management endpoints."""
from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from litworkbench.cloud_auth import FLOW_COOKIE, SESSION_COOKIE, SESSION_TTL
from litworkbench.web.errors import error_response

router = APIRouter()


def safe_return(value: str) -> str:
    if re.fullmatch(r'(?:/|/pages(?:/[A-Za-z0-9_-]+)?)(?:\?[^\x00-\x20\\#]*)?', value):
        return value
    return '/pages'


def configure_oidc(settings):
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    return oauth.register(
        name='owner', client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=settings.oidc_issuer + '/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid', 'code_challenge_method': 'S256', 'timeout': 15},
    )


@router.get('/auth/login')
async def login(request: Request, return_to: str = '/pages'):
    settings = request.app.state.settings
    if settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '本地模式不使用云端登录。')
    client = request.app.state.oidc
    try:
        metadata = await client.load_server_metadata()
        if metadata.get('issuer') != settings.oidc_issuer:
            raise ValueError('issuer mismatch')
        request.scope['session'] = {'return_to': safe_return(return_to)}
        response = await client.authorize_redirect(request, settings.public_url + '/auth/callback')
        old = request.cookies.get(FLOW_COOKIE)
        if old:
            request.app.state.cloud_auth.take_flow(old)
        flow = request.app.state.cloud_auth.save_flow(request.session)
        response.set_cookie(FLOW_COOKIE, flow, secure=True, httponly=True, samesite='lax', max_age=600, path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception:
        return error_response(503, 'OIDC_UNAVAILABLE', '暂时无法连接登录服务，请稍后重试。')


@router.get('/auth/callback')
async def callback(request: Request):
    settings = request.app.state.settings
    if settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '本地模式不使用云端登录。')
    auth = request.app.state.cloud_auth
    flow = auth.take_flow(request.cookies.get(FLOW_COOKIE))
    try:
        if not flow:
            raise ValueError('flow expired or replayed')
        request.scope['session'] = flow
        token = await request.app.state.oidc.authorize_access_token(
            request, claims_options={'iss': {'essential': True, 'value': settings.oidc_issuer}}, leeway=30)
        user = token.get('userinfo') or {}
        if (user.get('iss') != settings.oidc_issuer or not isinstance(user.get('sub'), str)
                or not hmac.compare_digest(user['sub'], settings.owner_subject)):
            raise ValueError('owner mismatch')
        session = auth.issue_session()
        response = RedirectResponse(safe_return(flow.get('return_to', '/pages')), status_code=303)
        response.set_cookie(SESSION_COOKIE, session.token, secure=True, httponly=True,
                            samesite='lax', path='/', max_age=SESSION_TTL)
    except Exception:
        response = error_response(400, 'OIDC_FAILED', '登录校验失败或账户不匹配，请重新登录。')
    response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.post('/auth/logout')
def logout(request: Request):
    if request.app.state.settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '本地模式没有云端会话。')
    request.app.state.cloud_auth.revoke_session(request.cookies.get(SESSION_COOKIE, ''))
    response = JSONResponse({'ok': True})
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite='lax', path='/')
    return response


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    scope: str = 'read'
    days: int = Field(default=90, ge=1, le=365)


@router.get('/api/v1/agent-tokens')
def list_tokens(request: Request):
    if request.app.state.settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '请在云端模式管理 Agent 令牌。')
    return {'items': request.app.state.cloud_auth.list_pats()}


@router.post('/api/v1/agent-tokens', status_code=201)
def create_token(request: Request, body: TokenCreate):
    if request.app.state.settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '请在云端模式管理 Agent 令牌。')
    return JSONResponse(request.app.state.cloud_auth.issue_pat(body.name, body.scope, days=body.days),
                        status_code=201, headers={'Cache-Control': 'no-store'})


@router.delete('/api/v1/agent-tokens/{token_id}')
def revoke_token(request: Request, token_id: str):
    if request.app.state.settings.mode != 'cloud':
        return error_response(404, 'NOT_FOUND', '请在云端模式管理 Agent 令牌。')
    request.app.state.cloud_auth.revoke_pat(token_id)
    return {'ok': True}
