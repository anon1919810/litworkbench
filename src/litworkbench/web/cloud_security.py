"""Global cloud guard; proxy headers never determine identity or public origin."""
import hmac

from litworkbench.cloud_auth import SESSION_COOKIE
from litworkbench.web.errors import error_response
from litworkbench.web.security import WRITE_METHODS


def check_cloud_request(request, auth, settings):
    if request.headers.get('host', '').lower() not in settings.allowed_hosts:
        return error_response(403, 'HOST_REJECTED', '请求地址不在允许的域名中。')
    path, method = request.url.path, request.method.upper()
    if path == '/mcp' or path.startswith('/mcp/'):
        header = request.headers.get('authorization', '')
        token = header[7:] if header.lower().startswith('bearer ') else ''
        principal = auth.get_pat(token)
        if not principal:
            return error_response(401, 'TOKEN_REQUIRED', 'MCP 需要有效的 Agent 令牌。')
        if request.headers.get('origin') not in (None, settings.public_url):
            return error_response(403, 'ORIGIN_REJECTED', '拒绝跨站 MCP 请求。')
        request.state.principal = principal
        return None
    protected = path.startswith('/api/') and path != '/api/v1/health' or path == '/auth/logout'
    if not protected:
        return None
    session = auth.get_session(request.cookies.get(SESSION_COOKIE))
    if not session:
        return error_response(401, 'LOGIN_REQUIRED', '请登录后访问工作区。', details={'login_url': '/auth/login'})
    request.state.principal = session.principal
    if method in WRITE_METHODS:
        csrf = request.headers.get('x-csrf-token', '')
        if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
            return error_response(403, 'CSRF_INVALID', '写入令牌无效，请重新连接。')
        if request.headers.get('origin') not in (None, settings.public_url) or request.headers.get('sec-fetch-site') == 'cross-site':
            return error_response(403, 'ORIGIN_REJECTED', '拒绝跨站写请求。')
    return None
