"""Workspace/Profile-bound connections; policy reuse never merges identities."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.permissions import connection_policy, project_scopes
from shared.workspace_contracts import WorkspaceModel
from shared.util import DevError


class ConnectionCreate(WorkspaceModel):
    name: str = Field(min_length=1, max_length=80)
    profile_id: str = Field(pattern=r'^prf_[a-f0-9]{32}$')
    profile_version: int = Field(ge=1)
    consent_mode: Literal['fixed', 'live'] = 'fixed'
    confirm_live_profile: bool = False
    confirm_external_mcp: bool = False
    days: int = Field(default=30, ge=1, le=365)
    idempotency_key: str = Field(min_length=8, max_length=128)


def effective_access(runtime, principal):
    projects = []
    for row in runtime.list_projects(principal):
        actions = project_scopes(runtime.store, principal, row['id'])
        configured = actions.copy()
        if row['mode'] != 'write':
            configured -= {'write', 'execute'}
        if not row['allow_tasks']:
            configured.discard('execute')
        projects.append({'id': row['id'], 'name': row['alias'], 'actions': sorted(configured),
                         'policy_actions': sorted(actions), 'online': row['online'],
                         'local_authorization_required': True})
    vps = runtime.vps.list({'project': '', 'limit': 200, 'offset': 0}, principal)['vps']
    from hub.gateway.policy import visible_tools
    mcp = {}
    if runtime.gateway and runtime.gateway.enabled:
        for binding, tool in visible_tools(runtime.store, principal):
            item = mcp.setdefault(binding['id'], {'id': binding['id'], 'name': binding['alias'], 'tools': []})
            item['tools'].append(tool['name'])
    return {'projects': projects, 'vps': vps, 'mcp': list(mcp.values())}


def public_connection(runtime, row):
    profile = runtime.store.one('SELECT id,label,version,enabled FROM access_profiles WHERE id=? AND space_id=?',
                                (row['profile_id'], row['space_id']))
    expires = row.get('expires')
    status = ('revoked' if row['revoked'] else 'expired' if expires and expires <= time.time()
              else 'pending' if not expires else 'active')
    policy, reason = {}, ''
    if status == 'active':
        try:
            policy = connection_policy(runtime.store, row).model_dump()
            if not profile or not profile['enabled']:
                status = 'disabled'
        except DevError as exc:
            status, reason = 'disabled', exc.message
    return {'id': row['id'], 'name': row['label'], 'client_id': row['client_id'],
            'profile': profile, 'status': status, 'expires': expires, 'created': row['created'],
            'consent_mode': row['consent_mode'], 'follows_profile_changes': row['consent_mode'] == 'live',
            'permissions': policy, 'unavailable_reason': reason}


def make_connections_router(auth, runtime):
    router, store = APIRouter(), runtime.store

    @router.get('/api/spaces/{space_id}/connections')
    @database_endpoint(store)
    def connections(request: Request):
        principal = auth.panel(request)
        rows = store.all('''SELECT g.*,(SELECT max(expires) FROM tokens t WHERE t.grant_id=g.id
            AND t.kind IN ('pat','access','refresh')) AS expires FROM grants g
            WHERE g.user_id=? AND g.space_id=? ORDER BY g.created DESC,g.id''',
            (principal.user_id, principal.space_id))
        return {'connections': [public_connection(runtime, row) for row in rows]}

    @router.post('/api/spaces/{space_id}/connections', status_code=201)
    @database_endpoint(store)
    def create(request: Request, body: ConnectionCreate):
        principal = auth.panel(request, True)
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={'idempotency_key'}), sort_keys=True).encode()).hexdigest()
        with store.transaction():
            principal = auth.panel(request, True)
            old = store.one('SELECT * FROM client_connection_requests WHERE space_id=? AND user_id=? AND request_key=?',
                            (principal.space_id, principal.user_id, body.idempotency_key))
            if old:
                if old['fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', 'The request key already identifies a different connection', 409)
                return {'grant_id': old['grant_id'], 'connection_id': old['grant_id'], 'replayed': True,
                        'note': 'The credential was returned only on the first response. The original connection was not recreated.'}
            result = auth.issue_connection(principal, body.name, body.profile_id, body.profile_version,
                body.days, consent_mode=body.consent_mode, confirm_live_profile=body.confirm_live_profile,
                confirm_external_mcp=body.confirm_external_mcp)
            store.db.execute('INSERT INTO client_connection_requests VALUES(?,?,?,?,?)',
                (principal.space_id, principal.user_id, body.idempotency_key, fingerprint, result['grant_id']))
            iam.audit(store, principal, 'connection.created', result['grant_id'],
                      detail={'profile_id': body.profile_id, 'consent_mode': body.consent_mode})
        return {**result, 'profile_id': body.profile_id, 'replayed': False}

    @router.post('/api/spaces/{space_id}/connections/{connection_id}/revoke')
    @database_endpoint(store)
    def revoke(connection_id: str, request: Request):
        principal = auth.panel(request, True)
        with store.transaction():
            row = store.one('SELECT * FROM grants WHERE id=? AND space_id=?', (connection_id, principal.space_id))
            if not row:
                raise DevError('CONNECTION_NOT_FOUND', 'Connection not found', 404)
            if row['user_id'] != principal.user_id:
                iam.require_space_capability(store, principal, 'connections')
            store.db.execute('UPDATE grants SET revoked=1 WHERE id=?', (connection_id,))
            iam.audit(store, principal, 'connection.revoked', connection_id)
        runtime.wake_delivery()
        return {'id': connection_id, 'revoked': True}
    return router
