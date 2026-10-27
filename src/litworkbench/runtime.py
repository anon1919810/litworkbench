"""Explicit local/cloud configuration. Secrets are never included in repr/errors."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str = 'local'
    public_url: str = ''
    allowed_hosts: tuple[str, ...] = ()
    oidc_issuer: str = ''
    oidc_client_id: str = ''
    oidc_client_secret: str = field(default='', repr=False)
    owner_subject: str = field(default='', repr=False)
    session_secret: str = field(default='', repr=False)
    token_secret: str = field(default='', repr=False)
    worker_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> 'RuntimeSettings':
        env = os.environ if env is None else env
        mode = env.get('LIT_MODE', 'local')
        if mode not in {'local', 'cloud'}:
            raise ValueError('LIT_MODE must be local or cloud')
        if mode == 'local':
            return cls()
        names = ('PUBLIC_URL', 'ALLOWED_HOSTS', 'OIDC_ISSUER', 'OIDC_CLIENT_ID',
                 'OIDC_CLIENT_SECRET', 'OWNER_SUBJECT', 'SESSION_SECRET', 'TOKEN_SECRET')
        values = {name: env.get('LIT_' + name, '').strip() for name in names}
        missing = [name for name in names if not values[name]]
        if missing:
            raise ValueError('Missing cloud configuration: ' + ', '.join('LIT_' + n for n in missing))
        public = values['PUBLIC_URL'].rstrip('/')
        issuer = values['OIDC_ISSUER'].rstrip('/')
        for name, url in [('PUBLIC_URL', public), ('OIDC_ISSUER', issuer)]:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or (name == 'PUBLIC_URL' and parsed.path)):
                raise ValueError('Invalid LIT_' + name)
        hosts = tuple(host.strip().lower() for host in values['ALLOWED_HOSTS'].split(','))
        if (urlsplit(public).netloc.lower() not in hosts
                or any(not host or any(c in host for c in '/*?#@ \\') for host in hosts)):
            raise ValueError('Invalid LIT_ALLOWED_HOSTS; use exact public authorities')
        if any(len(values[name].encode()) < 32 for name in ('SESSION_SECRET', 'TOKEN_SECRET')):
            raise ValueError('Cloud secrets must each contain at least 32 bytes')
        if values['SESSION_SECRET'] == values['TOKEN_SECRET']:
            raise ValueError('Session and token secrets must differ')
        worker = env.get('LIT_WORKER_ENABLED', 'false')
        if worker not in {'true', 'false'}:
            raise ValueError('LIT_WORKER_ENABLED must be true or false')
        return cls(mode, public, hosts, issuer, values['OIDC_CLIENT_ID'],
                   values['OIDC_CLIENT_SECRET'], values['OWNER_SUBJECT'],
                   values['SESSION_SECRET'], values['TOKEN_SECRET'], worker == 'true')
