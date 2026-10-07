"""One human setup boundary over stable Profiles, Grants and token issuance."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, ValidationError

from hub import iam
from hub.access import AccessModel
from hub.db_worker import database_endpoint
from hub.roles import (_policy, created_ids, project_actions, role_project_scopes,
                       RolePolicy, ProjectRule)
from shared.role_contracts import ROLE_SCOPE
from shared.util import DevError


class ConnectionCreate(AccessModel):
    name: str = Field(min_length=1, max_length=80)
    role_id: str = Field(min_length=1, max_length=100)
    expected_role_version: int = Field(ge=1)
    authorization_mode: Literal['fixed', 'role'] = 'fixed'
    confirm_dynamic_role: bool = False
    confirm_external_mcp: bool = False
    days: int = Field(default=30, ge=1, le=365)
    idempotency_key: str = Field(min_length=8, max_length=128)
    profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    profile_version: int | None = Field(default=None, ge=1)


def frozen_policy(store, role):
    current = _policy(role)
    created = created_ids(store, role['id'])
    groups = {}
    for row in store.all('SELECT id FROM projects WHERE space_id=?', (role['space_id'],)):
        actions = tuple(sorted(project_actions(current, row['id'], created)))
        if actions:
            groups.setdefault(actions, []).append(row['id'])
    try:
        return RolePolicy(project_rules=[{'actions': list(actions), 'projects': ids[i:i+1000]}
                for actions, ids in groups.items() for i in range(0, len(ids), 1000)],
                vps_rules=[r.model_dump() for r in current.vps_rules],
                connector_rules=[r.model_dump() for r in current.connector_rules])
    except ValidationError as exc:
        raise DevError('CONNECTION_RESOURCE_LIMIT', '固定连接的资源快照过大；请缩小角色中的明确资源范围', 409) from exc


def effective_access(runtime, principal):
    """Concrete pairs, after live policy checks; local Agent opt-ins remain gates."""
    projects = []
    for row in runtime.list_projects(principal):
        actions = role_project_scopes(runtime.store, principal, row['id'])
        configured = actions.copy()
        if row['mode'] != 'write':
            configured -= {'write', 'execute'}
        if not row['allow_tasks']:
            configured.discard('execute')
        projects.append({'id': row['id'], 'name': row['alias'], 'actions': sorted(configured),
                         'policy_actions': sorted(actions), 'online': row['online'],
                         'local_authorization_required': True})
    vps = []
    inventory, offset = [], 0
    while offset is not None:
        page = runtime.vps.list({'project': '', 'limit': 200, 'offset': offset}, principal)
        inventory.extend(page['vps'])
        offset = page['next_offset']
    for row in inventory:
        actions = set(row['actions'])
        route = row['execution_route']
        executable = row['can_execute']
        if not executable:
            actions.discard('execute')
        vps.append({'id': row['id'], 'name': row['name'], 'actions': sorted(actions),
                    'policy_actions': row['actions'], 'execution_route': route,
                    'enabled': row['enabled'], 'account_execution': True})
    from hub.gateway.policy import visible_tools
    mcp = {}
    if runtime.gateway and runtime.gateway.enabled:
        for binding, tool in visible_tools(runtime.store, principal):
            item = mcp.setdefault(binding['id'], {'id': binding['id'], 'name': binding['alias'],
                                  'account_id': binding['account_id'], 'tools': []})
            item['tools'].append(tool['name'])
    return {'projects': projects, 'vps': vps, 'mcp': list(mcp.values())}


def public_connection(runtime, row):
    store = runtime.store
    profile = store.one('SELECT id,label,enabled FROM access_profiles WHERE id=?', (row['profile_id'],)) if row['profile_id'] else None
    role = store.one('SELECT id,label,version,enabled FROM access_roles WHERE id=?', (row['role_id'],)) if row['role_id'] else None
    client = store.one('SELECT name FROM oauth_clients WHERE id=?', (row['client_id'],)) if row['client_id'] else None
    expires = row.get('expires')
    status = ('revoked' if row['revoked'] else 'expired' if expires and expires <= time.time()
              else 'pending' if not expires else 'active')
    permissions, reason = {'projects': [], 'vps': [], 'mcp': []}, ''
    if status == 'active':
        try:
            caller = runtime.grant_principal(row)
            permissions = effective_access(runtime, caller)
            if profile and not profile['enabled'] or role and not role['enabled']:
                status = 'disabled'
        except DevError as exc:
            status, reason = 'disabled', exc.message
    return {'id': row['id'], 'name': row['label'], 'client': client['name'] if client else 'Bearer client',
            'client_id': row['client_id'], 'profile': profile, 'role': role, 'status': status,
            'expires': expires, 'created': row['created'], 'authorization_mode': row['authorization_mode'],
            'follows_role_changes': row['authorization_mode'] == 'role',
            'fixed_resource_snapshot': bool(row.get('resource_policy')), 'permissions': permissions,
            'unavailable_reason': reason}


def make_connections_router(auth, runtime):
    router, store = APIRouter(prefix='/api/client-connections'), runtime.store

    @router.get('')
    @database_endpoint(store)
    def connections(request: Request):
        principal = auth.panel(request)
        rows = store.all('''SELECT g.*,(SELECT max(expires) FROM tokens t WHERE t.grant_id=g.id
            AND t.kind IN ('pat','access','refresh')) AS expires FROM grants g
            WHERE g.user_id=? AND g.space_id=? ORDER BY g.created DESC,g.id''',
            (principal.user_id, principal.space_id))
        return {'connections': [public_connection(runtime, row) for row in rows]}

    @router.post('', status_code=201)
    @database_endpoint(store)
    def create(request: Request, body: ConnectionCreate):
        principal = auth.panel(request, True)
        label = body.name.strip()
        if not label or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise DevError('INVALID_CONNECTION', '客户端名称不能为空或包含控制字符')
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={'idempotency_key'}), sort_keys=True).encode()).hexdigest()
        with store.transaction():
            principal = auth.panel(request, True)
            old = store.one('SELECT * FROM client_connection_requests WHERE space_id=? AND user_id=? AND request_key=?',
                            (principal.space_id, principal.user_id, body.idempotency_key))
            if old:
                if old['fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '请求键已用于其他连接配置', 409)
                return {'grant_id': old['grant_id'], 'replayed': True,
                        'note': '连接已建立；凭据只在首次响应中显示。请核对原连接，不会重复创建。'}
            role = iam.role_eligible(store, principal.user_id, body.role_id, principal.space_id)
            if not role['enabled'] or role['version'] != body.expected_role_version:
                raise DevError('ROLE_CHANGED', '角色已改变或暂停；请重新阅读资源规则', 409)
            if body.authorization_mode == 'role' and not body.confirm_dynamic_role:
                raise DevError('DYNAMIC_CONSENT_REQUIRED', '须明确同意此角色的当前及未来资源与能力变化', 400)
            snapshot = frozen_policy(store, role) if body.authorization_mode == 'fixed' else _policy(role)
            from hub.gateway.catalog import fingerprint as tool_fingerprint
            tool_ceiling = {}
            for rule in snapshot.connector_rules:
                binding = store.one('SELECT tools FROM gateway_bindings WHERE id=? AND space_id=?', (rule.binding_id, principal.space_id))
                if binding:
                    tool_ceiling[rule.binding_id] = {t['name']: tool_fingerprint(t) for t in json.loads(binding['tools']) if t['name'] in rule.tools}
            scopes = sorted({'read'} | {a for rule in snapshot.project_rules for a in rule.actions}
                            | {'execute' for rule in snapshot.vps_rules if 'execute' in rule.actions})
            projects = sorted({pid for rule in snapshot.project_rules for pid in rule.projects}) if body.authorization_mode == 'fixed' else []
            if body.profile_id:
                profile = store.one('SELECT * FROM access_profiles WHERE id=? AND user_id=? AND space_id=?',
                                    (body.profile_id, principal.user_id, principal.space_id))
                if (not profile or not profile['enabled'] or profile['role_id'] != role['id']
                        or profile['version'] != body.profile_version):
                    raise DevError('PROFILE_CHANGED', '高级身份绑定已改变，请重新核对', 409)
            else:
                from hub.access_profiles import MAX_PROFILES
                if store.one('SELECT count(*) AS n FROM access_profiles WHERE user_id=?', (principal.user_id,))['n'] >= MAX_PROFILES:
                    raise DevError('PROFILE_LIMIT', '身份数量达到上限；可在高级设置复用已有身份', 409)
                identifier, now = 'prf_' + uuid.uuid4().hex, time.time()
                identity_label = label[:60] + ' · ' + identifier[-8:]
                import unicodedata
                identity_key = unicodedata.normalize('NFKC', identity_label).casefold()
                store.db.execute('''INSERT INTO access_profiles(id,user_id,label,label_key,scopes,projects,
                    enabled,version,created,updated,create_key,create_fingerprint,role_id,space_id,owner_user_id)
                    VALUES(?,?,?,?,?,?,1,1,?,?,?,?,?,?,?)''',
                    (identifier, principal.user_id, identity_label, identity_key, json.dumps(scopes), json.dumps(projects),
                     now, now, 'connection:' + body.idempotency_key, fingerprint, role['id'], principal.space_id, principal.user_id))
                profile = store.one('SELECT * FROM access_profiles WHERE id=?', (identifier,))
                iam.audit(store, principal, 'profile.created', identifier, detail={'source': 'client_connection', 'role_id': role['id']})
            result = auth.issue_grant(principal, label,
                [ROLE_SCOPE] if body.authorization_mode == 'role' else scopes,
                [] if body.authorization_mode == 'role' else projects, body.days,
                profile_id=profile['id'], profile_version=profile['version'],
                authorization_mode=body.authorization_mode, role_version=role['version'],
                confirm_dynamic_role=body.confirm_dynamic_role, confirm_external_mcp=body.confirm_external_mcp,
                resource_policy=snapshot.model_dump() if body.authorization_mode == 'fixed' else None)
            store.db.execute('INSERT INTO client_connection_requests VALUES (?,?,?,?,?)',
                (principal.space_id, principal.user_id, body.idempotency_key, fingerprint, result['grant_id']))
            if body.authorization_mode == 'fixed':
                store.db.execute('UPDATE grants SET connector_ceiling=? WHERE id=?', (json.dumps(tool_ceiling, sort_keys=True), result['grant_id']))
            iam.audit(store, principal, 'client_connection.created', result['grant_id'], detail={
                'role_id': role['id'], 'authorization_mode': body.authorization_mode, 'profile_id': profile['id']})
        return {**result, 'profile_id': profile['id'], 'replayed': False}

    return router
