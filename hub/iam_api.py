"""Explicit workspace administration with direct member policies, not Roles."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.permissions import AccessPolicy, _policy, owner_policy, validate_policy, member_policy, intersect_policies
from hub.workspace_mutations import label_key, mutate, version
from shared.crypto import token, digest
from shared.workspace_contracts import WorkspaceModel
from shared.util import DevError


class SpaceCreate(WorkspaceModel):
    label: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=128)


class SpaceEdit(WorkspaceModel):
    label: str = Field(min_length=1, max_length=100)
    active: bool = True
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class MemberEdit(WorkspaceModel):
    policy: AccessPolicy
    active: bool = True
    expected_version: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)


class UserEdit(WorkspaceModel):
    active: bool = True
    instance_admin: bool = False
    expected_version: int = Field(ge=1)


class InviteCreate(WorkspaceModel):
    policy: AccessPolicy
    days: int = Field(default=7, ge=1, le=30)


class InviteAccept(WorkspaceModel):
    invitation: str = Field(min_length=20, max_length=200, repr=False)


class BlockEdit(WorkspaceModel):
    blocked: bool


class ShareEdit(WorkspaceModel):
    visibility: Literal['private', 'space']


def spaces_for(store, user_id):
    rows = store.all('''SELECT DISTINCT s.* FROM spaces s JOIN memberships m ON m.space_id=s.id
        WHERE m.user_id=? AND m.active=1 AND s.active=1 AND (m.expires IS NULL OR m.expires>?) ORDER BY s.created,s.id''', (user_id, time.time()))
    result = []
    for row in rows:
        try:
            member = iam.membership(store, user_id, row['id'])
        except DevError:
            continue
        result.append({**row, 'management': member['policy']['manage']})
    return result


def delegation_guard(store, principal, policy):
    validate_policy(store, principal.space_id, policy)
    if principal.admin:
        return
    own = member_policy(store, principal.user_id, principal.space_id)
    requested = intersect_policies(store, principal.space_id, policy)
    effective = intersect_policies(store, principal.space_id, own, policy)
    if (not set(policy.manage) <= set(own.manage) or requested.model_dump() != effective.model_dump()
            or any(r.all_projects for r in policy.project_rules) or any(r.all_vps for r in policy.vps_rules)):
        raise DevError('DELEGATION_EXCEEDED', 'A member manager cannot delegate beyond their own direct workspace rights', 403)


def administrator_guard(store, space_id, user_id, policy, active):
    """Do not remove the final active holder of workspace management capability."""
    current = store.all('''SELECT m.* FROM memberships m JOIN iam_users u ON u.user_id=m.user_id
        WHERE m.space_id=? AND m.active=1 AND u.active=1 AND (m.expires IS NULL OR m.expires>?)
        AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.space_id=m.space_id AND b.user_id=m.user_id AND b.blocked=1)''', (space_id, time.time()))
    before = {row['user_id'] for row in current if 'workspace' in _policy(row).manage}
    after = {row['user_id'] for row in current if row['user_id'] != user_id and 'workspace' in _policy(row).manage}
    if active and 'workspace' in policy.manage:
        after.add(user_id)
    if user_id in before and not after:
        raise DevError('LAST_WORKSPACE_ADMIN', 'Keep at least one active member with direct workspace management rights', 409)


def make_iam_router(auth, runtime):
    router, store = APIRouter(), runtime.store

    def wake(principal, action, target='', detail=None):
        iam.audit(store, principal, action, target, detail=detail)
        runtime.publish('iam', {'space_id': principal.space_id, 'user_id': target or principal.user_id})
        runtime.wake_delivery()

    @router.get('/api/iam/me')
    @database_endpoint(store)
    def me(request: Request):
        session = auth.session(request)
        user = iam.user_security(store, session['user_id'])
        identity_access = None
        try:
            iam.check_identity(store, session.get('identity_id'))
        except DevError as exc:
            if exc.code not in {'ENTITLEMENTS_STALE', 'ENTITLEMENTS_UNAVAILABLE'}:
                raise
            identity_access = {'code': exc.code, 'message': exc.message, **exc.details}
        from hub.identity_status import public_identity
        identities = [public_identity(store, row) for row in store.all(
            'SELECT i.*,p.label,p.version AS provider_version FROM external_identities i JOIN oidc_providers p ON p.id=i.provider_id WHERE i.user_id=?', (user['id'],))]
        disabled = []
        for row in store.all('''SELECT DISTINCT s.* FROM spaces s JOIN memberships m ON m.space_id=s.id
                WHERE m.user_id=? AND m.active=1 AND s.active=0 AND (m.expires IS NULL OR m.expires>?)
                AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.space_id=m.space_id AND b.user_id=m.user_id AND b.blocked=1)''', (user['id'], time.time())):
            policies = store.all('SELECT policy FROM memberships WHERE user_id=? AND space_id=? AND active=1 AND (expires IS NULL OR expires>?)', (user['id'], row['id'], time.time()))
            if any('workspace' in _policy(p).manage for p in policies):
                disabled.append(row)
        return {'id': user['id'], 'username': user['username'], 'display_name': user['display_name'] or user['username'],
                'instance_admin': bool(user['instance_admin']), 'local_login': bool(user['local_login']),
                'spaces': spaces_for(store, user['id']), 'identities': identities, 'identity_access': identity_access,
                'session_expires': session['expires'], 'disabled_spaces': disabled}

    @router.get('/api/spaces')
    @database_endpoint(store)
    def spaces(request: Request):
        account = auth.panel(request)
        return {'spaces': spaces_for(store, account.user_id)}

    @router.get('/api/spaces/{space_id}')
    @database_endpoint(store)
    def workspace(space_id: str, request: Request):
        principal = auth.panel(request)
        row = store.one('SELECT * FROM spaces WHERE id=?', (space_id,))
        return {**row, 'management': member_policy(store, principal.user_id, space_id).manage}

    @router.post('/api/spaces', status_code=201)
    @database_endpoint(store)
    def create_space(request: Request, body: SpaceCreate):
        principal = auth.panel(request, True)
        def save(current):
            if len(spaces_for(store, current.user_id)) >= 100:
                raise DevError('WORKSPACE_LIMIT', 'The account workspace limit has been reached', 409)
            identifier = 'sp_' + uuid.uuid4().hex
            label, _ = label_key(body.label)
            store.db.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,?)', (identifier, label, current.user_id, time.time()))
            store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)', (identifier, current.user_id, owner_policy().model_dump_json()))
            wake(replace(current, space_id=identifier), 'workspace.created', identifier)
            return store.one('SELECT * FROM spaces WHERE id=?', (identifier,))
        return mutate(store, principal, 'workspace.create', body.idempotency_key, body.model_dump(), save)

    @router.put('/api/spaces/{space_id}')
    @database_endpoint(store)
    def edit_space(space_id: str, request: Request, body: SpaceEdit):
        principal = iam.require_space_capability(store, auth.panel(request, True), 'workspace')
        def save(current):
            iam.require_space_capability(store, current, 'workspace')
            row = store.one('SELECT * FROM spaces WHERE id=?', (space_id,))
            version(row, body.expected_version)
            label, _ = label_key(body.label)
            store.db.execute('UPDATE spaces SET label=?,active=?,version=version+1 WHERE id=?', (label, int(body.active), space_id))
            wake(current, 'workspace.updated', space_id)
            return store.one('SELECT * FROM spaces WHERE id=?', (space_id,))
        return mutate(store, principal, 'workspace.update:'+space_id, body.idempotency_key, body.model_dump(), save)

    @router.post('/api/spaces/{space_id}/restore')
    @database_endpoint(store)
    def restore_space(space_id: str, request: Request, body: SpaceEdit):
        with store.transaction():
            session = auth.session_write(request)
            iam.check_identity(store, session['identity_id'])
            row = store.one('SELECT * FROM spaces WHERE id=?', (space_id,))
            blocked = store.one('SELECT 1 FROM membership_blocks WHERE space_id=? AND user_id=? AND blocked=1', (space_id, session['user_id']))
            policies = store.all('SELECT policy FROM memberships WHERE user_id=? AND space_id=? AND active=1 AND (expires IS NULL OR expires>?)', (session['user_id'], space_id, time.time()))
            if blocked or not any('workspace' in _policy(p).manage for p in policies):
                raise DevError('WORKSPACE_NOT_FOUND', 'Workspace is not available for recovery by this account', 404)
            version(row, body.expected_version)
            store.db.execute('UPDATE spaces SET active=1,version=version+1 WHERE id=?', (space_id,))
            principal = auth.panel(request, True)
            wake(principal, 'workspace.restored', space_id)
        return store.one('SELECT * FROM spaces WHERE id=?', (space_id,))

    @router.get('/api/spaces/{space_id}/members')
    @database_endpoint(store)
    def members(space_id: str, request: Request):
        iam.require_space_capability(store, auth.panel(request), 'members')
        rows = store.all('''SELECT m.*,u.username,s.display_name,s.active AS user_active,COALESCE(b.blocked,0) AS blocked
            FROM memberships m JOIN users u ON u.id=m.user_id JOIN iam_users s ON s.user_id=u.id
            LEFT JOIN membership_blocks b ON b.space_id=m.space_id AND b.user_id=m.user_id
            WHERE m.space_id=? ORDER BY u.username,m.source''', (space_id,))
        return {'members': [{**row, 'policy': json.loads(row['policy'])} for row in rows]}

    @router.put('/api/spaces/{space_id}/members/{user_id}')
    @database_endpoint(store)
    def set_member(space_id: str, user_id: str, request: Request, body: MemberEdit):
        principal = iam.require_space_capability(store, auth.panel(request, True), 'members')
        def save(current):
            iam.require_space_capability(store, current, 'members')
            iam.user_security(store, user_id)
            old = store.one("SELECT * FROM memberships WHERE space_id=? AND user_id=? AND source='manual'", (space_id, user_id))
            if (old['version'] if old else 0) != body.expected_version:
                raise DevError('VERSION_CHANGED', 'The member policy changed; reload before saving', 409)
            if old and 'workspace' in _policy(old).manage and not current.admin:
                raise DevError('WORKSPACE_ADMIN_REQUIRED', 'Only a workspace administrator can change another administrator', 403)
            delegation_guard(store, current, body.policy)
            administrator_guard(store, space_id, user_id, body.policy, body.active)
            store.db.execute("""INSERT INTO memberships(space_id,user_id,source,policy,active) VALUES(?,?,'manual',?,?)
                ON CONFLICT(space_id,user_id,source) DO UPDATE SET policy=excluded.policy,active=excluded.active,version=memberships.version+1""",
                (space_id, user_id, body.policy.model_dump_json(), int(body.active)))
            wake(current, 'member.updated', user_id, body.model_dump(exclude={'idempotency_key'}))
            row = store.one("SELECT * FROM memberships WHERE space_id=? AND user_id=? AND source='manual'", (space_id, user_id))
            return {**row, 'policy': json.loads(row['policy'])}
        return mutate(store, principal, 'member.update:'+user_id, body.idempotency_key, body.model_dump(), save)

    @router.put('/api/spaces/{space_id}/members/{user_id}/suspension')
    @database_endpoint(store)
    def block_member(space_id: str, user_id: str, request: Request, body: BlockEdit):
        with store.transaction():
            principal = iam.require_space_capability(store, auth.panel(request, True), 'members')
            policies = store.all('SELECT policy FROM memberships WHERE space_id=? AND user_id=?', (space_id, user_id))
            if any('workspace' in _policy(p).manage for p in policies) and not principal.admin:
                raise DevError('WORKSPACE_ADMIN_REQUIRED', 'Only a workspace administrator can suspend another administrator', 403)
            if body.blocked:
                administrator_guard(store, space_id, user_id, AccessPolicy(), False)
            store.db.execute('INSERT INTO membership_blocks VALUES(?,?,?) ON CONFLICT(space_id,user_id) DO UPDATE SET blocked=excluded.blocked', (space_id, user_id, int(body.blocked)))
            wake(principal, 'member.suspension', user_id, body.model_dump())
        return {'blocked': body.blocked}

    @router.get('/api/spaces/{space_id}/invites')
    @database_endpoint(store)
    def invitations(space_id: str, request: Request):
        iam.require_space_capability(store, auth.panel(request), 'members')
        rows = store.all('SELECT hash AS id,policy,created_by,expires,used_by,created FROM space_invites WHERE space_id=? ORDER BY created DESC LIMIT 200', (space_id,))
        return {'invitations': [{**row, 'policy': json.loads(row['policy'])} for row in rows]}

    @router.delete('/api/spaces/{space_id}/invites/{identifier}')
    @database_endpoint(store)
    def revoke_invite(space_id: str, identifier: str, request: Request):
        with store.transaction():
            principal = iam.require_space_capability(store, auth.panel(request, True), 'members')
            store.db.execute('DELETE FROM space_invites WHERE hash=? AND space_id=?', (identifier, space_id))
            wake(principal, 'invitation.revoked', identifier)
        return {'ok': True}

    @router.post('/api/spaces/{space_id}/invites', status_code=201)
    @database_endpoint(store)
    def create_invite(space_id: str, request: Request, body: InviteCreate):
        with store.transaction():
            principal = iam.require_space_capability(store, auth.panel(request, True), 'members')
            delegation_guard(store, principal, body.policy)
            secret, now = 'cpi_'+token(), time.time()
            store.db.execute('INSERT INTO space_invites VALUES(?,?,?,?,?,NULL,?)', (digest(secret), space_id, body.policy.model_dump_json(), principal.user_id, now+body.days*86400, now))
            wake(principal, 'invitation.created', space_id, {'policy': body.policy.model_dump()})
        return {'invitation': secret, 'expires': now+body.days*86400, 'note': 'This invitation is displayed only once.'}

    @router.post('/api/iam/invites/accept')
    @database_endpoint(store)
    def accept_invite(request: Request, body: InviteAccept):
        with store.transaction():
            principal = auth.panel(request, True)
            row = store.one('SELECT * FROM space_invites WHERE hash=? AND expires>? AND used_by IS NULL', (digest(body.invitation), time.time()))
            if not row:
                raise DevError('INVITATION_INVALID', 'Invitation is invalid, expired or already used')
            from hub.principal import Principal
            creator = iam.live_principal(store, Principal('invitation:'+row['created_by'], row['created_by'], {'read'}, [], space_id=row['space_id']))
            iam.require_space_capability(store, creator, 'members')
            policy = _policy(row)
            delegation_guard(store, creator, policy)
            source = 'invite:'+digest(body.invitation)[:24]
            store.db.execute('INSERT INTO memberships(space_id,user_id,source,policy) VALUES(?,?,?,?)', (row['space_id'], principal.user_id, source, policy.model_dump_json()))
            store.db.execute('UPDATE space_invites SET used_by=? WHERE hash=?', (principal.user_id, row['hash']))
            wake(replace(principal, space_id=row['space_id']), 'invitation.accepted', principal.user_id)
        return {'space_id': row['space_id']}

    @router.get('/api/iam/users')
    @database_endpoint(store)
    def users(request: Request):
        auth.instance(request)
        return {'users': store.all('SELECT u.id,u.username,u.created,s.active,s.instance_admin,s.local_login,s.version,s.display_name FROM users u JOIN iam_users s ON s.user_id=u.id ORDER BY u.created')}

    @router.put('/api/iam/users/{user_id}')
    async def edit_user(user_id: str, request: Request, body: UserEdit):
        def update():
            with store.transaction():
                principal = auth.instance(request, True)
                row = store.one('SELECT * FROM iam_users WHERE user_id=?', (user_id,))
                version(row, body.expected_version)
                if row['active'] and row['instance_admin'] and (not body.active or not body.instance_admin):
                    if not store.one('SELECT 1 FROM iam_users WHERE user_id<>? AND instance_admin=1 AND active=1 LIMIT 1', (user_id,)):
                        raise DevError('LAST_ADMIN', 'Keep at least one active instance administrator', 409)
                    if row['local_login'] and not store.one('SELECT 1 FROM iam_users WHERE user_id<>? AND instance_admin=1 AND active=1 AND local_login=1 LIMIT 1', (user_id,)):
                        raise DevError('LAST_RECOVERY_ADMIN', 'Keep at least one local recovery administrator', 409)
                if not body.active:
                    for space in spaces_for(store, user_id):
                        if space['kind'] != 'personal':
                            administrator_guard(store, space['id'], user_id, AccessPolicy(), False)
                store.db.execute('UPDATE iam_users SET active=?,instance_admin=?,version=version+1,epoch=epoch+1 WHERE user_id=?', (int(body.active), int(body.instance_admin), user_id))
                store.db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
                store.db.execute('UPDATE grants SET revoked=1 WHERE user_id=?', (user_id,))
                wake(principal, 'user.updated', user_id, body.model_dump())
                return store.all('SELECT id FROM devices WHERE owner_user_id=?', (user_id,)) if not body.active else []
        for device in await store.run(update):
            await runtime.disconnect_device(device['id'], 'Owner account suspended')
        return {'id': user_id, **body.model_dump(exclude={'expected_version'}), 'version': body.expected_version+1}

    @router.get('/api/iam/sessions')
    @database_endpoint(store)
    def sessions(request: Request):
        session = auth.session(request)
        return {'sessions': store.all('''SELECT s.id_hash AS id,s.expires,ss.authenticated_at,ss.identity_id FROM sessions s
            LEFT JOIN session_security ss ON ss.session_hash=s.id_hash WHERE s.user_id=? AND s.expires>?''', (session['user_id'], time.time())), 'current': session['id_hash']}

    @router.delete('/api/iam/sessions/{identifier}')
    @database_endpoint(store)
    def delete_session(identifier: str, request: Request):
        with store.transaction():
            session = auth.session_write(request)
            store.db.execute('DELETE FROM sessions WHERE id_hash=? AND user_id=?', (identifier, session['user_id']))
            store.audit('panel:'+session['username'], 'session.revoked', commit=False)
        return {'ok': True}

    @router.put('/api/spaces/{space_id}/share/{kind}/{identifier}')
    @database_endpoint(store)
    def share(kind: str, identifier: str, request: Request, body: ShareEdit):
        if kind != 'artifact':
            raise DevError('INVALID_RESOURCE', 'Only artifacts can be shared; interactive sessions remain private')
        with store.transaction():
            principal = auth.panel(request, True)
            row = store.one('SELECT * FROM artifacts WHERE id=? AND space_id=?', (identifier, principal.space_id))
            if not row or row['owner_user_id'] != principal.user_id:
                raise DevError('RESOURCE_NOT_FOUND', 'Only the artifact owner can change its visibility', 404)
            runtime.project(row['project_id'], principal)
            store.db.execute('UPDATE artifacts SET visibility=? WHERE id=?', (body.visibility, identifier))
            wake(principal, 'record.shared', identifier, {'visibility': body.visibility})
        return {'id': identifier, 'visibility': body.visibility}
    return router
