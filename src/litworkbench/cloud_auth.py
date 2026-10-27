"""Revocable browser sessions, one-use OAuth flows, and scoped Agent tokens."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from litworkbench.errors import ValidationError
from litworkbench.runtime import RuntimeSettings
from litworkbench.store import Workspace

SESSION_COOKIE = '__Host-lit_session'
FLOW_COOKIE = '__Host-lit_oidc'
SESSION_TTL = 12 * 3600


@dataclass(frozen=True)
class Principal:
    owner_id: str
    actor_id: str
    origin: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class CloudSession:
    token: str
    csrf_token: str
    principal: Principal


class CloudAuth:
    def __init__(self, workspace: Workspace, settings: RuntimeSettings):
        self.workspace, self.settings = workspace, settings
        self.owner = hashlib.sha256((settings.oidc_issuer + '\0' + settings.owner_subject).encode()).hexdigest()

    def _digest(self, value: str, purpose: str) -> str:
        key = self.settings.token_secret if purpose == 'pat' else self.settings.session_secret
        return hmac.new(key.encode(), (purpose + '\0' + value).encode(), hashlib.sha256).hexdigest()

    def issue_session(self) -> CloudSession:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        with self.workspace.connect() as conn:
            conn.execute('DELETE FROM cloud_sessions WHERE expires_at < ?', (now,))
            conn.execute('INSERT INTO cloud_sessions VALUES (?, ?, ?, ?, ?)',
                         (self._digest(token, 'session'), self.owner, csrf, now, now + SESSION_TTL))
        return CloudSession(token, csrf, Principal(self.owner, 'owner', 'web', frozenset({'read', 'edit'})))

    def get_session(self, token: str | None) -> CloudSession | None:
        if not token or len(token) > 256:
            return None
        with self.workspace.connect() as conn:
            row = conn.execute('SELECT * FROM cloud_sessions WHERE digest=? AND owner_id=? AND expires_at>?',
                               (self._digest(token, 'session'), self.owner, time.time())).fetchone()
        return CloudSession(token, row['csrf'], Principal(self.owner, 'owner', 'web', frozenset({'read', 'edit'}))) if row else None

    def revoke_session(self, token: str) -> None:
        with self.workspace.connect() as conn:
            conn.execute('DELETE FROM cloud_sessions WHERE digest=?', (self._digest(token, 'session'),))

    def issue_pat(self, name: str, scope: str, *, days: int = 90) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValidationError('令牌名称须为 1–100 个字符。')
        if scope not in {'read', 'edit'} or type(days) is not int or not 1 <= days <= 365:
            raise ValidationError('令牌权限须为 read/edit，有效期为 1–365 天。')
        token, token_id, now = 'lit_' + secrets.token_urlsafe(32), secrets.token_hex(12), time.time()
        with self.workspace.connect() as conn:
            conn.execute('INSERT INTO agent_tokens (id,digest,owner_id,name,scope,created_at,expires_at) VALUES (?,?,?,?,?,?,?)',
                         (token_id, self._digest(token, 'pat'), self.owner, name.strip(), scope, now, now + days * 86400))
        return {'id': token_id, 'name': name.strip(), 'scope': scope, 'expires_at': now + days * 86400, 'token': token}

    def list_pats(self) -> list[dict]:
        with self.workspace.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT id,name,scope,created_at,expires_at,revoked_at,last_used_at FROM agent_tokens WHERE owner_id=? ORDER BY created_at DESC', (self.owner,))]

    def revoke_pat(self, token_id: str) -> None:
        with self.workspace.connect() as conn:
            conn.execute('UPDATE agent_tokens SET revoked_at=? WHERE id=? AND owner_id=?', (time.time(), token_id, self.owner))

    def get_pat(self, token: str | None) -> Principal | None:
        if not token or len(token) > 256:
            return None
        now = time.time()
        with self.workspace.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT id,scope FROM agent_tokens WHERE digest=? AND owner_id=? AND revoked_at IS NULL AND expires_at>?',
                               (self._digest(token, 'pat'), self.owner, now)).fetchone()
            if row:
                conn.execute('UPDATE agent_tokens SET last_used_at=? WHERE id=?', (now, row['id']))
        return Principal(self.owner, row['id'], 'codex', frozenset({'read', 'edit'} if row['scope'] == 'edit' else {'read'})) if row else None

    def save_flow(self, data: dict) -> str:
        token, now = secrets.token_urlsafe(32), time.time()
        with self.workspace.connect() as conn:
            conn.execute('DELETE FROM oauth_flows WHERE expires_at<?', (now,))
            conn.execute('INSERT INTO oauth_flows VALUES (?,?,?)', (self._digest(token, 'flow'), json.dumps(data), now + 600))
        return token

    def take_flow(self, token: str | None) -> dict | None:
        if not token or len(token) > 256:
            return None
        with self.workspace.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('DELETE FROM oauth_flows WHERE digest=? RETURNING *', (self._digest(token, 'flow'),)).fetchone()
        return json.loads(row['data']) if row and row['expires_at'] > time.time() else None
