"""Non-secret identity-renewal diagnostics, never an authorization override."""
from __future__ import annotations

import json
import time

from shared.crypto import digest

TRANSIENT_SYNC_CODES = frozenset({'OIDC_UPSTREAM_ERROR', 'OIDC_KEYS_UNAVAILABLE'})


def current_sync_state(store, identity):
    encrypted = identity.get('upstream_tokens')
    if not encrypted:
        return None
    return store.one("""SELECT * FROM oidc_sync_state WHERE identity_id=?
        AND credential_hash=? AND provider_version=? AND observed_checked_at=?""",
        (identity['id'], digest(encrypted), identity['provider_version'], identity['checked_at']))


def public_identity(store, identity):
    state = current_sync_state(store, identity)
    code = state['code'] if state else ''
    renewable = False
    try:
        secured = json.loads(store.decrypt(identity['upstream_tokens'])) if identity['upstream_tokens'] else {}
        renewable = isinstance(secured.get('refresh_token'), str) and bool(secured['refresh_token'])
    except Exception:
        # Never expose a cipher error or raw upstream data to the UI.
        code = 'OIDC_RECORD_INVALID'
    return {key: identity[key] for key in ('id', 'provider_id', 'label', 'enabled', 'fresh_until')} | {
        'checked_at': identity['checked_at'],
        'sync_code': code,
        'next_attempt': state['next_attempt'] if state else None,
        'has_refresh_token': renewable,
        'reauthentication_required': code == 'OIDC_REAUTH_REQUIRED',
        'permissions_fresh': identity['fresh_until'] > time.time(),
    }
