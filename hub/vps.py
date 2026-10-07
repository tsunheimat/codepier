"""Saved VPS inventory. Public metadata is separate from encrypted credentials.

The Hub stores passwords using its existing master key. Only the durable worker
resolves them for the encrypted Agent channel; model calls and queue payloads
contain references, never the password. Existing Agent ssh_exec handles process
permissions, streamed redaction, timeouts, cancellation and journal recovery.
"""
from __future__ import annotations
from hub.db_worker import database_endpoint

import ipaddress
import sqlite3
import time
import unicodedata
import uuid

from fastapi import APIRouter, Query, Request
from pydantic import Field, model_validator

from shared.contracts import Args, SSHExec, VPSList
from shared.util import DevError
from hub import iam
from hub.principal import refresh_principal
from hub.roles import connection_policy, vps_actions, role_project_scopes, _policy


PUBLIC_FIELDS = ('id', 'name', 'host', 'port', 'username', 'host_key_policy',
                 'provider', 'region', 'system', 'notes', 'enabled', 'version',
                 'connection_revision', 'created', 'updated', 'execution_project_id')
PUBLIC_SQL = ','.join('v.' + key for key in PUBLIC_FIELDS)


def name_key(value):
    return unicodedata.normalize('NFKC', value.strip()).casefold()


def canonical_host(value):
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.rstrip('.').lower()


class VPSInput(Args):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default='root', min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    host_key_policy: str = Field(default='strict', pattern=r'^(strict|accept-new)$')
    provider: str = Field(default='', max_length=120)
    region: str = Field(default='', max_length=120)
    system: str = Field(default='', max_length=160)
    notes: str = Field(default='', max_length=2000)
    enabled: bool = True
    project_ids: list[str] = Field(default_factory=list, max_length=500)
    execution_project_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_connection(self):
        self.name = self.name.strip()
        self.host = canonical_host(self.host)
        if not self.name or any(ord(c) < 32 for c in self.name):
            raise ValueError('VPS 名称不能为空或含控制字符')
        # Reuse the transport's host/user/password validation; never echo inputs.
        SSHExec(project='validation', host=self.host, port=self.port,
                username=self.username, password=self.password or 'validation-only',
                command='true', host_key_policy=self.host_key_policy,
                idempotency_key='validation-only')
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError('项目分配列表不能重复')
        return self


class VPSAssignments(Args):
    project_ids: list[str] = Field(max_length=500)
    expected_version: int = Field(ge=1)


class ProjectVPSAssignments(Args):
    vps_ids: list[str] = Field(max_length=500)
    expected_vps_ids: list[str] = Field(max_length=500)


class VPSService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def public(self, row, visible_ids=None):
        result = {key: row[key] for key in PUBLIC_FIELDS}
        result['enabled'] = bool(result['enabled'])
        result['has_password'] = True
        result['target'] = 'vps:' + row['id']
        projects = self.store.all('''SELECT p.id,p.alias,p.device_id,d.name AS device_name,
            p.mode,p.allow_tasks FROM vps_projects vp JOIN projects p ON p.id=vp.project_id
            JOIN devices d ON d.id=p.device_id WHERE vp.vps_id=? ORDER BY p.alias_key''', (row['id'],))
        result['projects'] = [{**p, 'online': self.runtime.online(p['device_id']),
                               'allow_tasks': bool(p['allow_tasks'])}
                              for p in projects if visible_ids is None or p['id'] in visible_ids]
        result['project_ids'] = [p['id'] for p in result['projects']]
        route = self.store.one('SELECT p.id,p.alias,p.device_id,d.name AS device_name,d.enabled AS device_enabled,p.mode,p.allow_tasks FROM projects p JOIN devices d ON d.id=p.device_id WHERE p.id=?', (row['execution_project_id'],))
        result['execution_route'] = ({**route, 'online': self.runtime.online(route['device_id']),
            'configured': True, 'available': route['mode'] == 'write' and bool(route['allow_tasks']) and bool(route['device_enabled'])}
            if route and (visible_ids is None or route['id'] in visible_ids) else {'configured': bool(route), 'available': False})
        if visible_ids is not None and row['execution_project_id'] not in visible_ids:
            result['execution_project_id'] = None
        return result

    def actions(self, identifier, principal):
        principal = refresh_principal(self.store, principal)
        if not self.store.one('SELECT id FROM vps_connections WHERE id=? AND space_id=?', (identifier, principal.space_id)):
            return set()
        if principal.admin and not principal.grant_id:
            return {'read', 'execute'}
        if principal.grant_id:
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
            policy = connection_policy(self.store, grant)
            return vps_actions(policy, identifier) if policy is not None else set()
        result = set()
        for role in iam.assigned_roles(self.store, principal.user_id, principal.space_id):
            if role['enabled']:
                result.update(vps_actions(_policy(role), identifier))
        return result

    def require(self, identifier, principal, action='read'):
        principal = refresh_principal(self.store, principal)
        if action not in self.actions(identifier, principal):
            raise DevError('VPS_POLICY_DENIED', '此连接未获明确 VPS ' + action + ' 权限；项目关联不会授予 SSH 访问', 403)
        return principal

    def can_execute(self, row, principal):
        if not row['enabled'] or not row['execution_project_id'] or 'execute' not in self.actions(row['id'], principal):
            return False
        try:
            project = self.runtime.project(row['execution_project_id'], principal)
            return (project['mode'] == 'write' and bool(project['allow_tasks']) and bool(project['device_enabled'])
                    and 'execute' in role_project_scopes(self.store, principal, project['id']))
        except DevError:
            return False

    def get(self, identifier, principal):
        principal=refresh_principal(self.store,principal)
        row = self.store.one(f'SELECT {PUBLIC_SQL} FROM vps_connections v WHERE v.id=? AND v.space_id=?', (identifier,principal.space_id))
        if not row:
            raise DevError('VPS_NOT_FOUND', '未找到 VPS 连接', 404)
        visible={p['id'] for p in self.runtime.list_projects(principal)}
        if 'read' not in self.actions(identifier, principal):
            raise DevError('VPS_NOT_FOUND','未找到已授权 VPS',404)
        result = self.public(row, None if principal.admin else visible)
        result['actions'] = sorted(self.actions(identifier, principal))
        result['can_execute'] = self.can_execute(row, principal)
        return result

    def manage(self,principal,identifier=None):
        principal=iam.live_principal(self.store,principal)
        if principal.grant_id or not principal.admin:
            raise DevError('SPACE_ADMIN_REQUIRED','VPS 管理需要当前空间管理员',403)
        if identifier and not self.store.one('SELECT 1 AS ok FROM vps_connections WHERE id=? AND space_id=?',(identifier,principal.space_id)):
            raise DevError('VPS_NOT_FOUND','未找到当前空间 VPS',404)
        return principal

    def list(self, args, principal):
        principal = refresh_principal(self.store, principal)
        projects = self.runtime.list_projects(principal)
        visible = {p['id'] for p in projects}
        chosen = self.runtime.project(args['project'], principal)['id'] if args.get('project') else None
        query = name_key(args.get('query', ''))
        permitted = {chosen} if chosen else None
        if permitted is not None and not permitted:
            return {'vps': [], 'total': 0, 'next_offset': None}
        params = tuple(sorted(permitted)) if permitted is not None else ()
        where = ' WHERE v.space_id=?'
        if permitted is not None:
            placeholders = ','.join('?' for _ in params)
            where += f' AND EXISTS (SELECT 1 FROM vps_projects vp WHERE vp.vps_id=v.id AND vp.project_id IN ({placeholders}))'
        rows = self.store.all(f'SELECT {PUBLIC_SQL} FROM vps_connections v{where} ORDER BY v.name_key,v.id', (principal.space_id,*params))
        results = []
        for row in rows:
            if 'read' not in self.actions(row['id'], principal):
                continue
            if query and not any(query in name_key(str(row[key])) for key in ('name', 'host', 'provider', 'region')):
                # IPv6 can be supplied in a different, equivalent representation.
                if canonical_host(args.get('query', '')) != row['host']:
                    continue
            results.append(row)
        offset, limit = args.get('offset', 0), args.get('limit', 50)
        end = offset + limit
        return {'vps': [{**self.public(row, None if principal.admin else visible), 'actions': sorted(self.actions(row['id'], principal)), 'can_execute': self.can_execute(row, principal)} for row in results[offset:end]], 'total': len(results),
                'next_offset': end if end < len(results) else None}

    def _validate_projects(self, identifiers, principal):
        self.store.require_transaction()
        if len(set(identifiers)) != len(identifiers):
            raise DevError('INVALID_PROJECTS', '项目分配列表不能重复')
        for identifier in identifiers:
            if not self.store.db.execute('SELECT 1 FROM projects WHERE id=? AND space_id=?', (identifier,principal.space_id)).fetchone():
                raise DevError('PROJECT_NOT_FOUND', '选择的项目已不存在，请刷新后重试', 404)

    def _bind(self, identifier, project_ids):
        self.store.require_transaction()
        existing = {r[0] for r in self.store.db.execute('SELECT project_id FROM vps_projects WHERE vps_id=?', (identifier,))}
        for project_id in existing - set(project_ids):
            self.store.db.execute('DELETE FROM vps_projects WHERE vps_id=? AND project_id=?', (identifier, project_id))
        for project_id in set(project_ids) - existing:
            self.store.db.execute('INSERT INTO vps_projects VALUES (?,?,?)', (identifier, project_id, uuid.uuid4().hex))

    def save(self, body, principal, identifier=None):
        principal=self.manage(principal,identifier)
        data = body.model_dump()
        existing_id = identifier
        identifier = identifier or uuid.uuid4().hex
        now = time.time()
        try:
            with self.store.lock, self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                principal=self.manage(principal,existing_id)
                old = self.store.db.execute('SELECT * FROM vps_connections WHERE id=?', (identifier,)).fetchone()
                if existing_id and not old:
                    raise DevError('VPS_NOT_FOUND', 'VPS 已删除，请刷新列表', 404)
                if old and body.expected_version != old['version']:
                    raise DevError('VPS_VERSION_CONFLICT', 'VPS 已在其他窗口修改，请重新打开后保存；未覆盖现有配置', 409)
                if not old and body.password is None:
                    raise DevError('VPS_PASSWORD_REQUIRED', '新建 VPS 需要填写 SSH 密码')
                self._validate_projects(body.project_ids,principal)
                route_id = body.execution_project_id
                # Older editors can update metadata without erasing a route.
                if old and 'execution_project_id' not in body.model_fields_set:
                    route_id = old['execution_project_id']
                if route_id:
                    route = self.store.one('SELECT mode,allow_tasks FROM projects WHERE id=? AND space_id=?', (route_id, principal.space_id))
                    if not route or route['mode'] != 'write' or not route['allow_tasks']:
                        raise DevError('VPS_ROUTE_INVALID', 'SSH 执行路线须明确选择当前空间可写且允许执行的项目 / Agent', 403)
                secret = self.store.encrypt(body.password) if body.password is not None else old['secret']
                version = old['version'] + 1 if old else 1
                connection_changed = old and (body.password is not None or route_id != old['execution_project_id'] or any(data[k] != old[k] for k in ('host', 'port', 'username', 'host_key_policy', 'enabled')))
                revision = old['connection_revision'] + int(bool(connection_changed)) if old else 1
                values = (body.name, name_key(body.name), body.host, body.port, body.username, secret,
                          body.host_key_policy, body.provider, body.region, body.system, body.notes,
                          int(body.enabled), version, revision, now)
                if old:
                    self.store.db.execute('''UPDATE vps_connections SET name=?,name_key=?,host=?,port=?,username=?,secret=?,
                        host_key_policy=?,provider=?,region=?,system=?,notes=?,enabled=?,version=?,connection_revision=?,updated=?
                        WHERE id=?''', (*values, identifier))
                else:
                    self.store.db.execute('''INSERT INTO vps_connections(name,name_key,host,port,username,secret,
                        host_key_policy,provider,region,system,notes,enabled,version,connection_revision,updated,id,created,space_id,owner_user_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (*values, identifier, now,principal.space_id,principal.user_id))
                self._bind(identifier, body.project_ids)
                self.store.db.execute('UPDATE vps_connections SET execution_project_id=? WHERE id=?', (route_id, identifier))
        except sqlite3.IntegrityError as exc:
            raise DevError('VPS_EXISTS', 'VPS 名称或相同地址、端口、账号的连接已存在', 409) from exc
        self.store.audit(principal.actor, 'vps.updated' if existing_id else 'vps.created', identifier,
                         detail={'project_ids': body.project_ids, 'version': version,
                                 'credential_changed': body.password is not None})
        self.runtime.publish('vps', {'id': identifier})
        return self.get(identifier,principal)

    def assign(self, identifier, body, principal):
        principal=self.manage(principal,identifier)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            principal=self.manage(principal,identifier)
            row = self.store.db.execute('SELECT version FROM vps_connections WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise DevError('VPS_NOT_FOUND', '未找到 VPS 连接', 404)
            if row['version'] != body.expected_version:
                raise DevError('VPS_VERSION_CONFLICT', '分配已被修改，请刷新后重试', 409)
            self._validate_projects(body.project_ids,principal)
            self._bind(identifier, body.project_ids)
            self.store.db.execute('UPDATE vps_connections SET version=version+1,updated=? WHERE id=?', (time.time(), identifier))
        self.store.audit(principal.actor, 'vps.assigned', identifier, detail={'project_ids': body.project_ids})
        self.runtime.publish('vps', {'id': identifier})
        return self.get(identifier,principal)

    def project_assign(self, project_id, body, principal):
        principal=self.manage(principal)
        project = self.runtime.project(project_id, principal)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            self.manage(principal)
            self._validate_projects([project['id']],principal)
            existing = {r[0] for r in self.store.db.execute('SELECT vps_id FROM vps_projects WHERE project_id=?', (project['id'],))}
            if existing != set(body.expected_vps_ids):
                raise DevError('VPS_VERSION_CONFLICT', '项目的 VPS 分配已变化，请刷新后重试', 409)
            target = set(body.vps_ids)
            if len(target) != len(body.vps_ids):
                raise DevError('INVALID_VPS', 'VPS 列表不能重复')
            for identifier in target:
                if not self.store.db.execute('SELECT 1 FROM vps_connections WHERE id=? AND space_id=?', (identifier,principal.space_id)).fetchone():
                    raise DevError('VPS_NOT_FOUND', '所选 VPS 已删除，请刷新后重试', 404)
            for identifier in existing - target:
                self.store.db.execute('DELETE FROM vps_projects WHERE project_id=? AND vps_id=?', (project['id'], identifier))
            for identifier in target - existing:
                self.store.db.execute('INSERT INTO vps_projects VALUES (?,?,?)', (identifier, project['id'], uuid.uuid4().hex))
            for identifier in existing ^ target:
                self.store.db.execute('UPDATE vps_connections SET version=version+1,updated=? WHERE id=?', (time.time(), identifier))
        self.store.audit(principal.actor, 'project.vps_assigned', project['id'], detail={'vps_ids': sorted(target)})
        self.runtime.publish('vps', {'project_id': project['id']})
        return {'ok': True, 'vps_ids': sorted(target)}

    def delete(self, identifier, version, principal):
        principal=self.manage(principal,identifier)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            principal=self.manage(principal,identifier)
            row = self.store.db.execute('SELECT version FROM vps_connections WHERE id=?', (identifier,)).fetchone()
            if not row:
                return {'ok': True}
            if row['version'] != version:
                raise DevError('VPS_VERSION_CONFLICT', 'VPS 已变化，请刷新后再删除', 409)
            self.store.db.execute('DELETE FROM vps_connections WHERE id=?', (identifier,))
        self.store.audit(principal.actor, 'vps.deleted', identifier, detail={'remote_server_deleted': False})
        self.runtime.publish('vps', {'id': identifier})
        return {'ok': True, 'note': '仅删除保存的连接及项目分配，不删除远程服务器；已下发的命令不会自动停止。'}

    def reference(self, args, project, principal):
        rows = self.store.all(f'SELECT {PUBLIC_SQL} FROM vps_connections v WHERE v.space_id=? AND v.enabled=1', (principal.space_id,))
        value = args.get('vps', '').strip()
        if value:
            exact_id = [r for r in rows if r['id'] == value]
            rows = exact_id or [r for r in rows if name_key(r['name']) == name_key(value) or r['host'] == canonical_host(value)]
        if args.get('port') is not None:
            rows = [r for r in rows if r['port'] == args['port']]
        if args.get('username'):
            rows = [r for r in rows if r['username'] == args['username']]
        if not rows:
            raise DevError('VPS_NOT_FOUND', '未找到启用的 VPS；请调用 vps 查询', 404)
        rows = [row for row in rows if 'read' in self.actions(row['id'], principal)]
        if not rows:
            raise DevError('VPS_POLICY_DENIED', '未明确授权此 VPS；关联项目不会授予权限', 403)
        if len(rows) != 1:
            choices = ', '.join(f"{r['name']} ({r['host']}:{r['port']} / {r['username']})" for r in rows[:10])
            raise DevError('VPS_AMBIGUOUS', '匹配到多个 VPS，请用 vps 查询并明确选择名称或 ID：' + choices, 409)
        row = rows[0]
        self.require(row['id'], principal, 'execute')
        if row['execution_project_id'] != project['id']:
            raise DevError('VPS_ROUTE_REQUIRED', '请使用 VPS 配置中明确选择的执行项目 / Agent；关联项目不是执行路线', 403)
        self.runtime.authorize(principal, 'execute', project_id=project['id'])
        return {'id': row['id'], 'connection_revision': row['connection_revision'], 'execution_project_id': project['id']}

    def _authorized_row(self, request, project_id, *, credential=False):
        ref = request.get('vps_ref') or {}
        columns = PUBLIC_SQL + (',v.secret' if credential else '')
        row = self.store.one(f'SELECT {columns} FROM vps_connections v WHERE v.id=?', (ref.get('id'),))
        if not row or not row['enabled'] or row['execution_project_id'] != project_id or ref.get('execution_project_id') != project_id:
            raise DevError('VPS_AUTHORIZATION_CHANGED', 'VPS 已停用、删除或执行路线未明确 / 已改变；尚未下发的命令不能继续执行', 403)
        if row['connection_revision'] != ref.get('connection_revision'):
            raise DevError('VPS_CONFIGURATION_CHANGED', 'VPS 连接或凭据已变化；请核对原操作后明确重新提交', 409)
        return row

    def permission_error(self, request, project_id, principal):
        try:
            self.require((request.get('vps_ref') or {}).get('id'), principal, 'execute')
            self._authorized_row(request, project_id)
        except DevError as exc:
            return exc.message
        return None

    def transport(self, request, project_id):
        row = self._authorized_row(request, project_id, credential=True)
        try:
            password = self.store.decrypt(row['secret'])
        except Exception as exc:
            raise DevError('VPS_CREDENTIAL_UNAVAILABLE', 'VPS 凭据无法解密，请检查 Hub 数据库与 master.key 是否配套', 409) from exc
        args = request['args']
        command = SSHExec(project=args['project'], host=row['host'], port=row['port'],
                          username=row['username'], password=password,
                          command=args['command'], host_key_policy=row['host_key_policy'],
                          timeout_seconds=args['timeout_seconds'], idempotency_key=args['idempotency_key'])
        envelope = {k: v for k, v in request.items() if k != 'vps_ref'}
        return {**envelope, 'core_ssh': command.model_dump()}


def make_vps_router(auth, runtime):
    router = APIRouter()
    service = runtime.vps

    @router.get('/api/vps')
    @database_endpoint(runtime.store)
    def list_vps(request: Request, project: str = Query('', max_length=100), query: str = Query('', max_length=253), offset: int = Query(0, ge=0, le=100000), limit: int = Query(200, ge=1, le=200)):
        principal = auth.panel(request)
        args = VPSList(project=project, query=query, offset=offset, limit=limit).model_dump()
        return service.list(args, principal)

    @router.post('/api/vps')
    @database_endpoint(runtime.store)
    def create_vps(request: Request, body: VPSInput):
        return service.save(body, auth.panel(request, True))

    @router.get('/api/vps/{identifier}')
    @database_endpoint(runtime.store)
    def get_vps(identifier: str, request: Request):
        return service.get(identifier,auth.panel(request))

    @router.put('/api/vps/{identifier}')
    @database_endpoint(runtime.store)
    def update_vps(identifier: str, request: Request, body: VPSInput):
        return service.save(body, auth.panel(request, True), identifier)

    @router.put('/api/vps/{identifier}/projects')
    @database_endpoint(runtime.store)
    def assign_vps(identifier: str, request: Request, body: VPSAssignments):
        return service.assign(identifier, body, auth.panel(request, True))

    @router.put('/api/projects/{identifier}/vps')
    @database_endpoint(runtime.store)
    def assign_project(identifier: str, request: Request, body: ProjectVPSAssignments):
        return service.project_assign(identifier, body, auth.panel(request, True))

    @router.delete('/api/vps/{identifier}')
    @database_endpoint(runtime.store)
    def delete_vps(identifier: str, request: Request, expected_version: int = Query(..., ge=1)):
        return service.delete(identifier, expected_version, auth.panel(request, True))

    return router
