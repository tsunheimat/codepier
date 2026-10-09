"""Opaque pagination cursors for durable record lists."""
import base64
import json

from shared.util import DevError


def encode_cursor(created, identifier):
    return base64.urlsafe_b64encode(json.dumps([created, identifier]).encode()).decode()


def decode_cursor(value):
    try:
        created, identifier = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        import math
        if type(created) not in (int, float) or not math.isfinite(created) or not isinstance(identifier, str) or len(identifier) != 32:
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise DevError("INVALID_CURSOR", "无效的分页游标；请重新读取第一页") from exc
