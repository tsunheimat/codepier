"""Transactional workspace writes with version checks and scoped idempotency."""
import hashlib
import json
import time
import unicodedata

from shared.util import DevError


def label_key(value):
    value = value.strip()
    if not value or any(ord(c)<32 or ord(c)==127 for c in value):
        raise DevError('INVALID_NAME', 'A name must be nonempty and contain no control characters')
    return value, unicodedata.normalize('NFKC', value).casefold()


def version(row, expected):
    if not row:
        raise DevError('RESOURCE_NOT_FOUND', 'Resource not found', 404)
    if type(expected) is not int or expected != row['version']:
        raise DevError('VERSION_CHANGED', 'The resource changed; reload it before saving', 409)


def mutate(store, principal, operation, request_key, payload, write):
    if not isinstance(request_key, str) or not 8 <= len(request_key) <= 128:
        raise DevError('IDEMPOTENCY_KEY_REQUIRED', 'An 8-128 character request key is required')
    body=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    fingerprint=hashlib.sha256(body.encode()).hexdigest()
    namespace=(principal.space_id or '',principal.user_id,operation,request_key)
    with store.transaction():
        from hub import iam
        principal=iam.live_principal(store,principal)
        old=store.one('SELECT fingerprint,receipt FROM workspace_requests WHERE space_id=? AND user_id=? AND operation=? AND request_key=?',namespace)
        if old:
            if old['fingerprint']!=fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT','The request key already identifies different arguments',409)
            return json.loads(old['receipt'])
        result=write(principal)
        store.db.execute('INSERT INTO workspace_requests VALUES(?,?,?,?,?,?,?)',(*namespace,fingerprint,json.dumps(result,ensure_ascii=False),time.time()))
        iam.audit(store,principal,operation,result.get('id',''),detail={'version':result.get('version')})
        return result
