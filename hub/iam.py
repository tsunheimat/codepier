"""Account identity and direct workspace membership. No ambient/current Space.

Panel account requests have no workspace. Resource pages carry explicit paths;
MCP calls always carry a server-authenticated, immutable connection binding.
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import replace

from hub.iam_read import read_decision, read_scope, memo, principal_key
from shared.workspace_contracts import CAPABILITIES
from shared.util import DevError

audit_context = ContextVar('codepier_audit_context', default=None)


def installed(store):
    return True


def membership(store, user_id, space_id):
    if not space_id:
        raise DevError('WORKSPACE_REQUIRED', 'This resource needs an explicit workspace', 400)
    space = store.one('SELECT * FROM spaces WHERE id=? AND active=1', (space_id,))
    if not space:
        raise DevError('SPACE_NOT_FOUND', 'Workspace not found', 404)
    if store.one('SELECT 1 AS blocked FROM membership_blocks WHERE user_id=? AND space_id=? AND blocked=1', (user_id, space_id)):
        raise DevError('SPACE_FORBIDDEN', 'Workspace membership is suspended', 403)
    rows = store.all('SELECT * FROM memberships WHERE user_id=? AND space_id=? AND active=1 AND (expires IS NULL OR expires>?)',
                     (user_id, space_id, time.time()))
    if not rows:
        raise DevError('SPACE_FORBIDDEN', 'No active membership in this workspace', 403)
    from hub.permissions import AccessPolicy, _policy
    policies = [_policy(row) for row in rows]
    fields = AccessPolicy.model_fields
    merged = {field: [value for policy in policies for value in getattr(policy, field)] for field in fields}
    merged['manage'] = sorted(set(merged['manage']))
    policy = AccessPolicy.model_construct(**merged)
    return {'space_id': space_id, 'user_id': user_id, 'policy': policy.model_dump(),
            'sources': [r['source'] for r in rows], 'version': max(r['version'] for r in rows)}


def is_space_admin(store, user_id, space_id):
    try:
        user_security(store, user_id)
        return 'workspace' in membership(store, user_id, space_id)['policy']['manage']
    except DevError:
        return False


def require_space_capability(store, principal, capability):
    principal = live_principal(store, principal)
    if principal.grant_id or not principal.space_id:
        raise DevError('WORKSPACE_ADMIN_REQUIRED', 'Workspace configuration requires a member panel session', 403)
    from hub.permissions import member_policy
    capabilities = member_policy(store, principal.user_id, principal.space_id).manage
    if 'workspace' not in capabilities and capability not in capabilities:
        raise DevError('WORKSPACE_ADMIN_REQUIRED', 'The member lacks this direct workspace management capability', 403)
    return principal


def validate_grant(store, grant):
    if not grant or grant.get('revoked') or not grant.get('space_id') or not grant.get('profile_id'):
        raise DevError('INVALID_TOKEN', 'Connection is revoked or has no workspace/profile binding', 401)
    user = user_security(store, grant['user_id'])
    if user['epoch'] != grant.get('user_epoch'):
        raise DevError('INVALID_TOKEN', 'Account credentials have been revoked', 401)
    identity = check_identity(store, grant.get('identity_id'))
    membership(store, grant['user_id'], grant['space_id'])
    if identity and identity['user_id'] != grant['user_id']:
        raise DevError('INVALID_TOKEN', 'External identity does not belong to this connection', 401)


@read_decision
def live_principal(store, principal):
    cache = memo(store)
    key = principal_key(principal)
    if cache is not None and key in cache:
        return cache[key]
    result = _live_principal(store, principal)
    if cache is not None:
        cache[key] = result
        cache[principal_key(result)] = result
    return result


def _live_principal(store, principal):
    from hub.principal import validate_credential
    principal = validate_credential(store, principal)
    user = user_security(store, principal.user_id)
    if principal.user_epoch is not None and user['epoch'] != principal.user_epoch:
        raise DevError('INVALID_TOKEN', 'Account credentials have been revoked', 401)
    identity = check_identity(store, principal.identity_id, require_fresh=bool(principal.space_id or principal.grant_id))
    if identity and identity['user_id'] != principal.user_id:
        raise DevError('INVALID_TOKEN', 'External identity does not belong to this account', 401)
    if principal.grant_id:
        grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
        validate_grant(store, grant)
        if any(grant[k] != getattr(principal, k) for k in ('user_id','space_id','profile_id')):
            raise DevError('INVALID_TOKEN', 'Connection identity cannot select a different workspace/profile', 401)
        return replace(principal, admin=False, instance_admin=False, user_epoch=user['epoch'])
    if principal.space_id is None:
        return replace(principal, admin=False, instance_admin=bool(user['instance_admin']),
                       user_epoch=user['epoch'], scopes={'read'}, projects=[])
    member = membership(store, principal.user_id, principal.space_id)
    from hub.permissions import AccessPolicy, policy_capabilities
    policy = AccessPolicy.model_validate(member['policy'])
    result = replace(principal, admin='workspace' in policy.manage,
                     instance_admin=bool(user['instance_admin']), user_epoch=user['epoch'])
    permissions = project_permissions(store, result)
    return replace(result, scopes=policy_capabilities(policy), projects=sorted(p for p,a in permissions.items() if 'read' in a))


@read_decision
def panel_workspaces(store, principal):
    """Enumerate independently authorized views; never change a login's identity.

    MCP connections remain one workspace. Instance administration is not implicit
    membership. Expired external entitlements stop disclosure rather than fall
    back to a wider account view.
    """
    principal = live_principal(store, principal)
    if principal.space_id is not None or principal.grant_id:
        return [principal]
    check_identity(store, principal.identity_id, require_fresh=True)
    from hub.iam_api import spaces_for
    return [live_principal(store, replace(principal, space_id=space['id']))
            for space in spaces_for(store, principal.user_id)]


@read_decision
def panel_access_snapshot(store, principal):
    """Monotonic resource/action atoms plus mapping revisions, never ambient state.

    Comparing serialized policy objects makes a harmless added Project appear
    to remove every former permission. Compare actual action/resource pairs
    instead. Removals still invalidate before emitting private stream data.
    """
    from hub.permissions import principal_policy
    result = set()
    for scoped in panel_workspaces(store, principal):
        policy = principal_policy(store, scoped)
        space=scoped.space_id
        result.add((space,'workspace','member'))
        result.update((space,'manage',action) for action in policy.manage)
        for project, actions in project_permissions(store, scoped).items():
            result.update((space,'project:'+project,action) for action in actions)
        # Keep future-resource selectors as explicit authority, without letting
        # a newly enumerated resource replace existing snapshot atoms.
        for rule in policy.project_rules:
            if rule.all_projects:
                selector='project:*:'+','.join(sorted(rule.excluded_projects))
                result.update((space,selector,action) for action in rule.actions)
        for rule in policy.device_rules:
            result.update((space,'device:'+device,action) for device in rule.devices for action in rule.actions)
        for rule in policy.source_rules:
            result.update((space,'source:'+folder,action) for folder in rule.sources for action in rule.actions)
        for rule in policy.vps_rules:
            result.update((space,'vps:'+vps,action) for vps in rule.vps for action in rule.actions)
            if rule.all_vps:result.update((space,'vps:*',action) for action in rule.actions)
        for rule in policy.connector_rules:
            result.update((space,'mcp:'+rule.binding_id,tool) for tool in rule.tools)
        for rule in policy.plugin_rules:
            for project in rule.projects if rule.scope=='project' else ['']:
                selector='plugin:'+rule.integration_id+':'+rule.scope+':'+project
                result.update((space,selector,action) for action in rule.actions)
        # Resource revisions invalidate stale mappings/editors even if the
        # account's action names have not changed.
        for table in ('space_sources', 'space_vps', 'access_profiles', 'projects'):
            for row in store.all('SELECT id,version FROM '+table+' WHERE space_id=?', (scoped.space_id,)):
                result.add((scoped.space_id, table+':'+row['id'], str(row['version'])))
        # Imports inherit live inventory settings. A changed Folder path, Pool
        # default or selected mapping must invalidate an open panel even when
        # its workspace binding and action names have not changed.
        for row in store.all('''
            SELECT 'folder:'||f.id AS resource,f.version FROM space_sources s
                JOIN folders f ON f.id=s.folder_resource_id WHERE s.space_id=?
            UNION
            SELECT 'pool:'||p.id,p.version FROM space_sources s
                JOIN storage_pools p ON p.id=s.pool_id WHERE s.space_id=?
            UNION
            SELECT 'mapping:'||m.id,m.version FROM space_source_mappings sm
                JOIN storage_mappings m ON m.id=sm.mapping_id WHERE sm.space_id=?
            ''', (scoped.space_id,scoped.space_id,scoped.space_id)):
            result.add((scoped.space_id,row['resource'],str(row['version'])))
    result.add(('@', 'instance_admin', str(bool(principal.instance_admin))))
    return frozenset(result)


@read_decision
def project_permissions(store, principal, *, elevated=None):
    if not principal.space_id:
        return {identifier: actions for scoped in panel_workspaces(store, principal)
                for identifier, actions in project_permissions(store, scoped).items()}
    from hub.permissions import principal_policy, project_actions
    policy = principal_policy(store, principal)
    return {row['id']: project_actions(policy, row['id'])
            for row in store.all('SELECT id FROM projects WHERE space_id=?', (principal.space_id,))}


@read_decision
def human_project_actions(store, principal, project_id):
    return set(project_permissions(store, principal).get(project_id, ()))


def project_in_space(store, principal, project_id):
    row = store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (project_id, principal.space_id))
    if not row:
        raise DevError('PROJECT_NOT_FOUND', 'Project is not provided by this workspace', 404)
    return row


@read_decision
def require_project(store, principal, action, project_id):
    from hub.permissions import require_access
    return require_access(store, principal, action, project_id=project_id)


def device_identity_active(store, device):
    if not device or not device.get('enabled') or not device.get('owner_user_id'):
        return False
    try:
        user_security(store, device['owner_user_id'])
        return True
    except DevError:
        return False


@read_decision
def require_device(store, principal, device_id, *, manage=False, creation=None):
    principal = live_principal(store, principal)
    row = store.one('SELECT * FROM devices WHERE id=?', (device_id,))
    if not row:
        raise DevError('DEVICE_NOT_FOUND', 'Device not found', 404)
    if principal.space_id:
        binding = store.one('SELECT enabled FROM space_devices WHERE space_id=? AND device_id=?', (principal.space_id, device_id))
        if not binding or not binding['enabled']:
            raise DevError('DEVICE_NOT_FOUND', 'Device is not provided by this workspace', 404)
    if manage:
        if principal.grant_id or not (principal.instance_admin or row['owner_user_id'] == principal.user_id):
            raise DevError('DEVICE_FORBIDDEN', 'Device inventory can only be managed by its owner or an instance administrator', 403)
        return row
    if principal.space_id:
        from hub.permissions import require_access
        if creation:
            require_access(store, principal, 'projects.create', source_id=creation.get('source_id'))
        else:
            require_access(store, principal, 'devices.read', device_id=device_id)
    elif principal.grant_id or not (principal.instance_admin or row['owner_user_id'] == principal.user_id):
        raise DevError('DEVICE_FORBIDDEN', 'Global inventory requires owner access', 403)
    return row


def record_visible(store, principal, row):
    if not row or row.get('space_id') != principal.space_id:
        return False
    if principal.grant_id:
        return row.get('grant_id') == principal.grant_id or row.get('visibility') == 'space'
    return principal.instance_admin or row.get('owner_user_id') == principal.user_id or row.get('visibility') == 'space'


@read_decision
def require_record(store, principal, row, *, kind='OPERATION'):
    if row and principal.space_id is None and not principal.grant_id and row.get('space_id'):
        principal = replace(principal, space_id=row['space_id'])
    principal = live_principal(store, principal)
    if not record_visible(store, principal, row):
        raise DevError(kind+'_NOT_FOUND', 'Record is not visible to this connection', 404)
    if row.get('project_id'):
        require_project(store, principal, 'read', row['project_id'])
    return principal


def scope_sql(principal, alias=''):
    return (f'{alias}space_id=?', [principal.space_id]) if principal.space_id is not None else (f'{alias}space_id IS NULL', [])



def owner_scope_sql(store, principal, alias='', *, owner_column='owner_user_id'):
    """Owner-private metadata across explicitly enumerated live workspaces.

    Sharing a Profile, resource, or workspace never shares connection history.
    This is for metadata lists, not permission to execute a referenced resource.
    """
    if owner_column not in {'owner_user_id', 'user_id'}:
        raise ValueError('Unknown owner column')
    spaces = [view.space_id for view in panel_workspaces(store, principal)]
    clause = alias+'space_id IN ('+(','.join('?' for _ in spaces) or 'NULL')+')'
    clause += ' AND '+alias+owner_column+'=?'
    args = [*spaces, principal.user_id]
    if principal.grant_id:
        clause += ' AND '+alias+'grant_id=?'
        args.append(principal.grant_id)
    return clause, args


def private_sql(principal, alias=''):
    clause, args = scope_sql(principal, alias)
    if principal.grant_id:
        clause += f" AND ({alias}grant_id=? OR {alias}visibility='space')"
        args.append(principal.grant_id)
    elif not principal.instance_admin:
        clause += f" AND ({alias}owner_user_id=? OR {alias}visibility='space')"
        args.append(principal.user_id)
    return clause, args


def audit(store, principal, action, target='', status='ok', detail=None):
    store.db.execute('INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?,?)',
        (time.time(), principal.actor, action, target, status, json.dumps(detail or {}, ensure_ascii=False), principal.space_id, principal.user_id))


def create_personal_space(store, user_id, label):
    from hub.space_bootstrap import ensure_personal_space
    return ensure_personal_space(store.db, user_id, label)


def user_security(store, user_id):
    row = store.one('SELECT u.*,s.active,s.instance_admin,s.local_login,s.epoch,s.version,s.display_name FROM users u JOIN iam_users s ON s.user_id=u.id WHERE u.id=?', (user_id,))
    if not row or not row['active']:
        raise DevError('ACCOUNT_DISABLED', '账号不存在或已停用', 401)
    return row

def check_identity(store, identity_id, *, require_fresh=True):
    if not identity_id:
        return None
    row = store.one('SELECT i.*,p.enabled AS provider_enabled,p.version AS provider_version FROM external_identities i JOIN oidc_providers p ON p.id=i.provider_id WHERE i.id=?', (identity_id,))
    if not row or not row['enabled'] or not row['provider_enabled']:
        raise DevError('IDENTITY_DISABLED', '外部身份或身份提供者已停用', 401)
    if require_fresh and row['fresh_until'] <= time.time():
        from hub.identity_status import current_sync_state, TRANSIENT_SYNC_CODES
        state = current_sync_state(store, row)
        code = state['code'] if state else ''
        if code in TRANSIENT_SYNC_CODES:
            raise DevError('ENTITLEMENTS_UNAVAILABLE',
                           '身份提供者暂不可用，权限校验正在重试；登录和 MCP 授权已保留', 503,
                           retryable=True, retry_after_seconds=10, sync_code=code)
        raise DevError('ENTITLEMENTS_STALE',
                       '身份权限尚未重新确认；登录和 MCP 授权已保留，请检查身份同步或重新认证', 403,
                       sync_code=code, reauthentication_required=code=='OIDC_REAUTH_REQUIRED')
    return row

def event_visible(runtime, principal, item):
    """Event contents are not emitted until current authorization is checked."""
    kind, data = item.get('type'), item.get('data') or {}
    if principal.space_id is None and not principal.grant_id:
        if kind == 'iam':
            return data.get('user_id') == principal.user_id or any(
                p.space_id == data.get('space_id') for p in panel_workspaces(runtime.store, principal))
        if kind == 'device':
            try:
                require_device(runtime.store, principal, data.get('id'))
                return True
            except DevError:
                return False
        from hub.resource_context import in_resource
        ref = None
        if kind == 'project':
            ref = ('projects', data.get('id'))
        elif kind in {'operation', 'operation_event', 'output', 'trace'}:
            ref = ('operations', data.get('id') or data.get('operation_id'))
        elif kind == 'vps':
            ref = ('space_vps', data.get('id')) if data.get('id') else ('projects', data.get('project_id'))
        elif kind == 'computer_approval':
            audience = item.get('_audience') or {}
            return audience.get('user_id') == principal.user_id and any(
                p.space_id == audience.get('space_id') for p in panel_workspaces(runtime.store, principal))
        if not ref or not ref[1]:
            return False
        try:
            scoped = in_resource(runtime.store, principal, *ref)
            return event_visible(runtime, scoped, item)
        except DevError:
            return False
    if kind == 'iam':
        return data.get('user_id') == principal.user_id or data.get('space_id') == principal.space_id
    if kind == 'device':
        try: require_device(runtime.store, principal, data.get('id')); return True
        except DevError: return False
    if kind == 'project':
        try: runtime.project(data.get('id'), principal); return True
        except DevError: return False
    if kind in {'operation', 'operation_event', 'output', 'trace'}:
        try: runtime.operation_row(data.get('id') or data.get('operation_id'), principal); return True
        except DevError: return False
    if kind == 'vps':
        try:
            if data.get('id'):runtime.vps.get(data['id'],principal)
            elif data.get('project_id'):runtime.project(data['project_id'],principal)
            else:return False
            return True
        except DevError:return False
    if kind == 'computer_approval':
        # An invalidation has no native content. Requiring a currently nonempty
        # inbox would suppress the last item's removal and leave stale controls.
        audience = item.get('_audience') or {}
        return (audience.get('space_id') == principal.space_id
                and (audience.get('user_id') == principal.user_id or principal.instance_admin))
    # Unknown/global notifications are not broadcast to arbitrary members.
    return False

class AuditContextMiddleware:
    def __init__(self, app):self.app=app
    async def __call__(self, scope, receive, send):
        key=audit_context.set(None)
        try:await self.app(scope,receive,send)
        finally:audit_context.reset(key)
