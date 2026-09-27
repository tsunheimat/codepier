"""Authenticated authority and credential fences shared by every backend."""
from dataclasses import dataclass, replace
import time
from shared.util import DevError


@dataclass
class Principal:
    actor: str
    user_id: str
    scopes: set[str]
    projects: list[str]
    grant_id: str | None = None
    admin: bool = False
    profile_id: str | None = None
    authorization_mode: str = 'fixed'
    role_id: str | None = None
    space_id: str = 'legacy'
    instance_admin: bool = False
    user_epoch: int | None = None
    identity_id: str | None = None
    session_hash: str = ''
    token_hash: str = ''


def validate_credential(store, principal):
    """No policy expansion: validate original credential after any scheduling wait."""
    if principal.session_hash:
        row = store.one('SELECT user_id FROM sessions WHERE id_hash=? AND expires>?',
                        (principal.session_hash, time.time()))
        if not row or row['user_id'] != principal.user_id:
            raise DevError('LOGIN_REQUIRED', '登录已过期或撤销', 401)
        if getattr(store, 'iam_enabled', False):
            security = store.one('SELECT epoch,identity_id FROM session_security WHERE session_hash=?',
                                 (principal.session_hash,))
            if not security or security['epoch'] != principal.user_epoch or security['identity_id'] != principal.identity_id:
                raise DevError('LOGIN_REQUIRED', '会话身份已变化', 401)
    if principal.token_hash:
        row = store.one("SELECT g.*,t.kind AS token_kind FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind IN ('access','pat') AND t.expires>? AND g.revoked=0",
                        (principal.token_hash, time.time()))
        if not row or row['id'] != principal.grant_id or row['user_id'] != principal.user_id:
            raise DevError('INVALID_TOKEN', '凭据已过期或撤销', 401)
        resource = getattr(store, 'oauth_resource', None)
        if row['token_kind'] == 'access' and resource and row['resource'] != resource():
            raise DevError('INVALID_TOKEN', '凭据的资源标识已改变', 401)
    return principal


def refresh_principal(store, principal):
    from hub.access_profiles import refresh_profile_principal
    return refresh_profile_principal(store, validate_credential(store, principal))
