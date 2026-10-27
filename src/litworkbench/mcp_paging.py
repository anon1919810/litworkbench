"""Signed, version-bound Unicode offsets and an actual serialized output budget."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from litworkbench.errors import EditConflictError, ValidationError


class CursorChanged(EditConflictError):
    code = 'CURSOR_CHANGED'


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def paginate(data, *, key: str, identity: str, revision=None, cursor: str | None = None,
             max_chars: int = 24000) -> dict:
    if type(max_chars) is not int or not 1024 <= max_chars <= 24000:
        raise ValidationError('max_chars must be between 1024 and 24000')
    text = _json(data)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    offset = 0
    if cursor:
        try:
            if len(cursor) > 2048: raise ValueError()
            encoded, signature = cursor.split('.')
            expected = hmac.new(key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature): raise ValueError()
            state = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if state['id'] != identity or state['exp'] < time.time(): raise ValueError()
            if state['hash'] != fingerprint or state['rev'] != revision:
                raise CursorChanged('内容已变化，请丢弃已读取片段并从头读取。')
            offset = state['offset']
            if type(offset) is not int or not 0 < offset < len(text): raise ValueError()
        except CursorChanged:
            raise
        except Exception:
            raise ValidationError('游标无效或已过期，请从头读取。') from None
    end = min(len(text), offset + max_chars - 700)
    while True:
        token = None
        if end < len(text):
            encoded = base64.urlsafe_b64encode(_json({'id': identity, 'rev': revision, 'hash': fingerprint,
                'offset': end, 'exp': int(time.time()) + 3600}).encode()).decode().rstrip('=')
            token = encoded + '.' + hmac.new(key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        result = {'encoding': 'json-fragment', 'revision': revision,
                  'text': text[offset:end], 'start_offset': offset, 'end_offset': end,
                  'total_characters': len(text), 'truncated': end < len(text), 'next_cursor': token,
                  'content_is_data': True}
        size = len(_json(result))
        if size <= max_chars:
            return result
        end -= max(1, (size - max_chars + 1) // 2)
        if end <= offset:
            raise ValidationError('输出预算不足，请增加 max_chars。')
