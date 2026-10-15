"""Space-owned NFS roots, route defaults, logical projects and test hooks.

All mutations are authenticated panel administration, never model-controlled
identity selection. Validation uses the selected device's existing local gates.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field, model_validator

from hub import iam
from hub.db_worker import database_endpoint
from hub.gateway.catalog import encoded, fingerprint
from shared.routing_contracts import (Contract, Family, FAMILIES, RouteSettings, SpaceExecutionSettings,
                                      absolute_mount, relative_source, mount_path)
from shared.util import DevError


class RootInput(Contract):
    expected_version: int = Field(ge=0)
    enabled: bool = True


class MountInput(Contract):
    path: str = Field(min_length=1, max_length=2048)
    mode: Literal['read', 'write'] = 'read'
    allow_tasks: bool = False
    expected_version: int = Field(ge=0)
    confirmed_same_storage: Literal[True]

    @model_validator(mode='after')
    def normalized(self):
        self.path = absolute_mount(self.path)
        if self.allow_tasks and self.mode != 'write':
            raise ValueError('Task mounts must be writable')
        return self


class RouteInput(Contract):
    family: Family
    device_id: str = Field(min_length=1, max_length=100)
    settings: RouteSettings = Field(default_factory=RouteSettings)
    expected_version: int = Field(ge=0)
    enabled: bool = True

    @model_validator(mode='after')
    def provider(self):
        if self.settings.provider == 'rch' and self.family != 'test':
            raise ValueError('RCH routes must use the test action')
        return self


class DefaultInput(Contract):
    route_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)


class ProjectSelection(Contract):
    alias: str = Field(min_length=1, max_length=64)
    subpath: str = Field(default='.', max_length=1024)
    mode: Literal['read', 'write'] = 'read'
    allow_tasks: bool = False
    description: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def normalized(self):
        self.subpath = relative_source(self.subpath)
        self.alias = self.alias.strip()
        if not re.fullmatch(r'[\w.-]{1,64}', self.alias) or self.alias in {'.', '..'}:
            raise ValueError('Project alias must be a name, not a filesystem path')
        if self.allow_tasks and self.mode != 'write':
            raise ValueError('Task projects must be writable')
        return self


class ProjectMetadata(ProjectSelection):
    expected_version: int = Field(ge=1)


class BulkProjects(Contract):
    source_root_id: str = Field(min_length=1, max_length=100)
    validation_device_id: str = Field(min_length=1, max_length=100)
    projects: list[ProjectSelection] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=128)


class SourceBinding(Contract):
    source_root_id: str | None = Field(default=None, max_length=100)
    subpath: str = Field(default='.', max_length=1024)
    validation_device_id: str = Field(default='', max_length=100)
    expected_version: int = Field(ge=1)

    @model_validator(mode='after')
    def valid(self):
        self.subpath = relative_source(self.subpath)
        if self.source_root_id and not self.validation_device_id:
            raise ValueError('Choose an authorized mount for subfolder validation')
        return self


class ProjectRoute(Contract):
    mode: Literal['inherit', 'disabled', 'route'] = 'inherit'
    route_id: str | None = Field(default=None, max_length=100)
    expected_version: int = Field(ge=1)

    @model_validator(mode='after')
    def selected(self):
        if (self.mode == 'route') != bool(self.route_id):
            raise ValueError('Only an explicit route override accepts a route ID')
        return self


class SettingsInput(Contract):
    settings: SpaceExecutionSettings
    expected_version: int = Field(ge=0)


class ReleaseHold(Contract):
    confirmed_remote_stopped: Literal[True]
    expected_state: Literal['needs_review']


def make_router(auth, runtime):
    router, store = APIRouter(), runtime.store

    def admin(request, write=False):
        return auth.admin(request, write)

    def source(identifier, principal):
        row = store.one('SELECT * FROM source_roots WHERE id=? AND space_id=?', (identifier, principal.space_id))
        if not row or not row['enabled']:
            raise DevError('SOURCE_NOT_FOUND', 'Shared source is not enabled in this space', 404)
        return row

    def mount(identifier, device, principal):
        source(identifier, principal)
        row = store.one('SELECT * FROM source_mounts WHERE source_root_id=? AND device_id=? AND space_id=?',
            (identifier, device, principal.space_id))
        if not row or not row['enabled']:
            raise DevError('SOURCE_MOUNT_UNAVAILABLE', 'Choose a validated source mount in this space', 409)
        iam.require_device(store, principal, device, manage=True)
        return row

    def route(identifier, family, principal):
        row = store.one('SELECT * FROM action_routes WHERE id=? AND space_id=?', (identifier, principal.space_id))
        if not row or row['family'] != family:
            raise DevError('ACTION_ROUTE_INVALID', 'Choose a route for this action in the current space', 400)
        return row

    def project(identifier, principal):
        iam.project_in_space(store, principal, identifier)
        return store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (identifier, principal.space_id))

    def version(row, expected):
        if (row['version'] if row else 0) != expected:
            raise DevError('ROUTING_CONFIG_CHANGED', 'Configuration changed; refresh before saving', 409)

    def changed(principal, action, identifier):
        store.audit(principal.actor, action, identifier, commit=False)
        store.after_commit(lambda: runtime.publish('project', {'id': identifier}))

    @router.get('/api/project-routing')
    @database_endpoint(store)
    def inventory(request: Request):
        principal = admin(request)
        roots = store.all('SELECT * FROM source_roots WHERE space_id=? ORDER BY name,id', (principal.space_id,))
        mounts = store.all('SELECT * FROM source_mounts WHERE space_id=? ORDER BY source_root_id,device_id', (principal.space_id,))
        routes = store.all('SELECT * FROM action_routes WHERE space_id=? ORDER BY name,id', (principal.space_id,))
        defaults = store.all('SELECT * FROM space_route_defaults WHERE space_id=?', (principal.space_id,))
        configured = store.one('SELECT * FROM space_execution_settings WHERE space_id=?', (principal.space_id,))
        devices = store.all('SELECT id,name,enabled,info FROM devices WHERE space_id=? ORDER BY name,id', (principal.space_id,))
        return {'sources': roots, 'mounts': mounts, 'routes': [{**r, 'settings': json.loads(r['settings'])} for r in routes],
            'defaults': defaults, 'actions': list(FAMILIES),
            'execution': {'settings': json.loads(configured['settings']) if configured else SpaceExecutionSettings().model_dump(),
                          'version': configured['version'] if configured else 0},
            'devices': [{'id': d['id'], 'name': d['name'], 'enabled': bool(d['enabled']), 'online': runtime.online(d['id'])} for d in devices]}

    @router.put('/api/source-roots/{name}')
    @database_endpoint(store)
    def save_source(name: str, request: Request, body: RootInput):
        principal = admin(request, True)
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', name):
            raise DevError('SOURCE_NAME_INVALID', 'Use a lowercase source name, not a device path', 400)
        with store.transaction():
            old = store.one('SELECT * FROM source_roots WHERE space_id=? AND name=?', (principal.space_id, name))
            version(old, body.expected_version)
            identifier = old['id'] if old else 'src_' + uuid.uuid4().hex
            if old:
                store.db.execute('UPDATE source_roots SET enabled=?,version=version+1 WHERE id=?', (int(body.enabled), identifier))
            else:
                if store.one('SELECT count(*) AS n FROM source_roots WHERE space_id=?', (principal.space_id,))['n'] >= 64:
                    raise DevError('SOURCE_LIMIT', 'This space already has 64 source roots', 409)
                store.db.execute('INSERT INTO source_roots(id,space_id,name,enabled,created) VALUES(?,?,?,?,?)',
                    (identifier, principal.space_id, name, int(body.enabled), time.time()))
            changed(principal, 'routing.source_saved', identifier)
            return store.one('SELECT * FROM source_roots WHERE id=?', (identifier,))

    @router.put('/api/source-roots/{identifier}/mounts/{device_id}')
    async def save_mount(identifier: str, device_id: str, request: Request, body: MountInput):
        def prepare():
            principal = admin(request, True)
            src = source(identifier, principal)
            iam.require_device(store, principal, device_id, manage=True)
            old = store.one('SELECT * FROM source_mounts WHERE source_root_id=? AND device_id=?', (identifier, device_id))
            version(old, body.expected_version)
            return principal, src, old
        principal, src, old = await store.run(prepare)
        validation = {'device_id': device_id, 'root': body.path, 'alias': 'source-validation', 'mode': body.mode,
                      'allow_tasks': body.allow_tasks}
        key = 'mount-' + fingerprint([principal.space_id, principal.user_id, identifier, device_id, body.model_dump()])
        result = await runtime.dispatch('system_validate', {'idempotency_key': key}, validation, principal)
        def finish():
            fresh = admin(request, True)
            if (fresh.user_id, fresh.space_id) != (principal.user_id, principal.space_id):
                raise DevError('ROUTING_IDENTITY_CHANGED', 'The active identity changed during validation', 403)
            with store.transaction():
                if source(identifier, fresh) != src:
                    raise DevError('ROUTING_CONFIG_CHANGED', 'The source changed during validation', 409)
                iam.require_device(store, fresh, device_id, manage=True)
                latest = store.one('SELECT * FROM source_mounts WHERE source_root_id=? AND device_id=?', (identifier, device_id))
                if latest != old:
                    raise DevError('ROUTING_CONFIG_CHANGED', 'The mount changed during validation', 409)
                if result.get('pending'):
                    raise DevError('VALIDATION_PENDING', 'Continue the original mount validation; no mount was saved', 409, operation_id=result['operation_id'])
                if body.mode == 'write' and not result.get('writable') or body.allow_tasks and not result.get('allow_tasks'):
                    raise DevError('SOURCE_LOCAL_GATE', 'The device did not authorize the requested mount capabilities', 403)
                path = absolute_mount(result['root'])
                mid = old['id'] if old else 'mnt_' + uuid.uuid4().hex
                store.db.execute('''INSERT INTO source_mounts(id,source_root_id,space_id,device_id,path,mode,allow_tasks,validated_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_root_id,device_id) DO UPDATE SET path=excluded.path,
                    mode=excluded.mode,allow_tasks=excluded.allow_tasks,enabled=1,validated_at=excluded.validated_at,version=source_mounts.version+1''',
                    (mid, identifier, fresh.space_id, device_id, path, body.mode, int(body.allow_tasks), time.time()))
                changed(fresh, 'routing.mount_saved', mid)
                return store.one('SELECT * FROM source_mounts WHERE id=?', (mid,))
        return await store.run(finish)

    @router.delete('/api/source-roots/{identifier}/mounts/{device_id}')
    @database_endpoint(store)
    def disable_mount(identifier: str, device_id: str, request: Request, expected_version: int = Query(ge=1)):
        principal = admin(request, True)
        with store.transaction():
            row = mount(identifier, device_id, principal)
            version(row, expected_version)
            store.db.execute('UPDATE source_mounts SET enabled=0,version=version+1 WHERE id=?', (row['id'],))
            changed(principal, 'routing.mount_disabled', row['id'])
        return {'ok': True, 'files_deleted': False, 'device_deleted': False}

    @router.get('/api/source-roots/{identifier}/browse')
    async def browse(identifier: str, request: Request, device_id: str, path: str = '.', offset: int = Query(default=0, ge=0, le=100000)):
        principal = await store.run(admin, request)
        selected = await store.run(mount, identifier, device_id, principal)
        try:
            relative = relative_source(path)
        except ValueError as exc:
            raise DevError('SOURCE_PATH_INVALID', str(exc), 400) from exc
        pseudo = {'device_id': device_id, 'root': selected['path'], 'alias': 'source-browser', 'mode': 'read', 'allow_tasks': False}
        args = {'project': 'source-browser', 'path': relative, 'depth': 1, 'offset': offset, 'limit': 200}
        result = await runtime.dispatch('fs_tree', args, pseudo, principal)
        fresh = await store.run(admin, request)
        latest = await store.run(mount, identifier, device_id, fresh)
        if (fresh.user_id, fresh.space_id) != (principal.user_id, principal.space_id) or latest != selected:
            raise DevError('ROUTING_CONFIG_CHANGED', 'Source or identity changed while browsing', 409)
        return result

    @router.put('/api/action-routes/{name}')
    @database_endpoint(store)
    def save_route(name: str, request: Request, body: RouteInput):
        principal = admin(request, True)
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', name):
            raise DevError('ROUTE_NAME_INVALID', 'Use a lowercase route name', 400)
        with store.transaction():
            iam.require_device(store, principal, body.device_id, manage=True)
            old = store.one('SELECT * FROM action_routes WHERE name=? AND space_id=?', (name, principal.space_id))
            version(old, body.expected_version)
            if old and old['family'] != body.family:
                raise DevError('ROUTE_ACTION_IMMUTABLE', 'Create another route to change its action family', 409)
            rid = old['id'] if old else 'route_' + uuid.uuid4().hex
            if not old and store.one('SELECT count(*) AS n FROM action_routes WHERE space_id=?', (principal.space_id,))['n'] >= 128:
                raise DevError('ROUTE_LIMIT', 'This space already has 128 action routes', 409)
            store.db.execute('''INSERT INTO action_routes(id,space_id,name,family,device_id,settings,enabled) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(space_id,name) DO UPDATE SET device_id=excluded.device_id,settings=excluded.settings,
                enabled=excluded.enabled,version=action_routes.version+1''',
                (rid, principal.space_id, name, body.family, body.device_id, encoded(body.settings.model_dump()), int(body.enabled)))
            changed(principal, 'routing.route_saved', rid)
            row = store.one('SELECT * FROM action_routes WHERE id=?', (rid,))
            return {**row, 'settings': json.loads(row['settings'])}

    @router.put('/api/project-routing/defaults/{family}')
    @database_endpoint(store)
    def save_default(family: Family, request: Request, body: DefaultInput):
        principal = admin(request, True)
        with store.transaction():
            route(body.route_id, family, principal)
            old = store.one('SELECT * FROM space_route_defaults WHERE space_id=? AND family=?', (principal.space_id, family))
            version(old, body.expected_version)
            store.db.execute('''INSERT INTO space_route_defaults(space_id,family,route_id) VALUES(?,?,?)
                ON CONFLICT(space_id,family) DO UPDATE SET route_id=excluded.route_id,version=space_route_defaults.version+1''',
                (principal.space_id, family, body.route_id))
            changed(principal, 'routing.default_saved', body.route_id)
            return store.one('SELECT * FROM space_route_defaults WHERE space_id=? AND family=?', (principal.space_id, family))

    async def validate_subfolder(principal, selected, subpath, mode, key):
        pseudo = {'device_id': selected['device_id'], 'root': mount_path(selected['path'], subpath),
            'alias': 'source-project-validation', 'mode': mode, 'allow_tasks': False,
            '_source_boundary': {'mount': selected['path'], 'subpath': subpath, 'protocol': 1}}
        result = await runtime.dispatch('system_validate', {'idempotency_key': key}, pseudo, principal)
        if result.get('pending'):
            raise DevError('VALIDATION_PENDING', 'Subfolder validation is pending; continue the same save request', 409, operation_id=result['operation_id'])
        if mode == 'write' and not result.get('writable'):
            raise DevError('SOURCE_READ_ONLY', 'The selected subfolder is not writable on this mount', 403)
        return result

    @router.post('/api/project-routing/projects')
    async def create_projects(request: Request, body: BulkProjects):
        from hub.runtime import alias_key
        def prepare():
            principal = admin(request, True)
            selected = mount(body.source_root_id, body.validation_device_id, principal)
            digest = fingerprint(body.model_dump())
            prior = store.one('SELECT * FROM routing_requests WHERE space_id=? AND user_id=? AND request_key=?',
                (principal.space_id, principal.user_id, body.idempotency_key))
            if prior and prior['fingerprint'] != digest:
                raise DevError('IDEMPOTENCY_CONFLICT', 'This request key belongs to another batch', 409)
            return principal, selected, digest, json.loads(prior['result']) if prior else None
        principal, selected, digest, prior = await store.run(prepare)
        if prior is not None:
            return {**prior, 'replayed': True}
        semaphore = asyncio.Semaphore(4)
        async def validate(index, item):
            async with semaphore:
                return await validate_subfolder(principal, selected, item.subpath, item.mode,
                    'source-batch-' + fingerprint([principal.space_id, principal.user_id, body.idempotency_key, digest, index]))
        await asyncio.gather(*(validate(i, item) for i, item in enumerate(body.projects)))
        def finish():
            fresh = admin(request, True)
            if (fresh.user_id, fresh.space_id) != (principal.user_id, principal.space_id):
                raise DevError('ROUTING_IDENTITY_CHANGED', 'Identity changed; no projects were created', 403)
            with store.transaction():
                if mount(body.source_root_id, body.validation_device_id, fresh) != selected:
                    raise DevError('ROUTING_CONFIG_CHANGED', 'Source mount changed; no projects were created', 409)
                prior = store.one('SELECT * FROM routing_requests WHERE space_id=? AND user_id=? AND request_key=?',
                    (fresh.space_id, fresh.user_id, body.idempotency_key))
                if prior:
                    if prior['fingerprint'] != digest:
                        raise DevError('IDEMPOTENCY_CONFLICT', 'Request key changed during validation', 409)
                    return {**json.loads(prior['result']), 'replayed': True}
                keys = [alias_key(p.alias) for p in body.projects]
                if len(set(keys)) != len(keys):
                    raise DevError('ALIAS_EXISTS', 'Project aliases in the batch must be unique', 409)
                result = []
                for item in body.projects:
                    if store.one('SELECT id FROM projects WHERE space_id=? AND alias_key=?', (fresh.space_id, alias_key(item.alias))):
                        raise DevError('ALIAS_EXISTS', 'An alias already exists; the entire batch was not applied', 409)
                    identifier = uuid.uuid4().hex
                    store.db.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created,
                        space_id,owner_user_id,routing_managed,source_root_id,subpath) VALUES(?,?,?,NULL,NULL,?,?,?,?,?,?,1,?,?)''',
                        (identifier, item.alias, alias_key(item.alias), item.description, item.mode, int(item.allow_tasks),
                         time.time(), fresh.space_id, fresh.user_id, body.source_root_id, item.subpath))
                    changed(fresh, 'routing.project_created', identifier)
                    result.append({'id': identifier, 'alias': item.alias, 'subpath': item.subpath})
                value = {'projects': result, 'source_root_id': body.source_root_id, 'replayed': False}
                store.db.execute('INSERT INTO routing_requests VALUES(?,?,?,?,?)',
                    (fresh.space_id, fresh.user_id, body.idempotency_key, digest, encoded(value)))
                return value
        return await store.run(finish)

    @router.get('/api/projects/{identifier}/routing')
    @database_endpoint(store)
    def project_routes(identifier: str, request: Request):
        principal = auth.panel(request)
        row = runtime.project(identifier, principal)
        overrides = store.all('SELECT * FROM project_action_routes WHERE project_id=?', (row['id'],))
        execution = store.one('SELECT * FROM project_execution_settings WHERE project_id=?', (row['id'],))
        return {'project': runtime.project_public(row), 'overrides': overrides,
            'execution': {'settings': json.loads(execution['settings']) if execution else SpaceExecutionSettings().model_dump(),
                          'version': execution['version'] if execution else 0}}

    @router.put('/api/projects/{identifier}/metadata')
    @database_endpoint(store)
    def metadata(identifier: str, request: Request, body: ProjectMetadata):
        from hub.runtime import alias_key
        principal = admin(request, True)
        with store.transaction():
            row = project(identifier, principal)
            if not row['routing_managed'] or row['routing_version'] != body.expected_version:
                raise DevError('ROUTING_CONFIG_CHANGED', 'Refresh the managed project before editing', 409)
            other = store.one('SELECT id FROM projects WHERE space_id=? AND alias_key=? AND id<>?',
                (principal.space_id, alias_key(body.alias), identifier))
            if other:
                raise DevError('ALIAS_EXISTS', 'This project alias is already used in the space', 409)
            store.db.execute('UPDATE projects SET alias=?,alias_key=?,description=?,mode=?,allow_tasks=?,routing_version=routing_version+1 WHERE id=?',
                (body.alias, alias_key(body.alias), body.description, body.mode, int(body.allow_tasks), identifier))
            changed(principal, 'routing.project_metadata_saved', identifier)
            return runtime.project_public(runtime.project(identifier, principal))

    @router.put('/api/projects/{identifier}/source')
    async def bind_source(identifier: str, request: Request, body: SourceBinding):
        def prepare():
            principal = admin(request, True)
            row = project(identifier, principal)
            if row['routing_version'] != body.expected_version:
                raise DevError('ROUTING_CONFIG_CHANGED', 'Project changed; refresh before rebinding', 409)
            selected = mount(body.source_root_id, body.validation_device_id, principal) if body.source_root_id else None
            return principal, row, selected
        principal, row, selected = await store.run(prepare)
        if selected:
            await validate_subfolder(principal, selected, body.subpath, row['mode'],
                'source-bind-' + fingerprint([principal.space_id, principal.user_id, identifier, body.model_dump()]))
        def finish():
            fresh = admin(request, True)
            if (fresh.user_id, fresh.space_id) != (principal.user_id, principal.space_id):
                raise DevError('ROUTING_IDENTITY_CHANGED', 'Identity changed during validation', 403)
            with store.transaction():
                if project(identifier, fresh) != row:
                    raise DevError('ROUTING_CONFIG_CHANGED', 'Project changed during validation', 409)
                if selected and mount(body.source_root_id, body.validation_device_id, fresh) != selected:
                    raise DevError('ROUTING_CONFIG_CHANGED', 'Mount changed during validation', 409)
                store.db.execute('''UPDATE projects SET routing_managed=1,device_id=NULL,root=NULL,source_root_id=?,subpath=?,
                    routing_version=routing_version+1 WHERE id=?''', (body.source_root_id, body.subpath, identifier))
                changed(fresh, 'routing.source_bound' if selected else 'routing.source_unbound', identifier)
                return {'id': identifier, 'routing_version': row['routing_version'] + 1,
                        'source_root_id': body.source_root_id, 'files_deleted': False, 'resources_deleted': False}
        return await store.run(finish)

    @router.put('/api/projects/{identifier}/routing/{family}')
    @database_endpoint(store)
    def override_route(identifier: str, family: Family, request: Request, body: ProjectRoute):
        principal = admin(request, True)
        with store.transaction():
            row = project(identifier, principal)
            if not row['routing_managed'] or row['routing_version'] != body.expected_version:
                raise DevError('ROUTING_CONFIG_CHANGED', 'Adopt the project and refresh its routing version before editing', 409)
            if body.mode == 'route':
                route(body.route_id, family, principal)
            if body.mode == 'inherit':
                store.db.execute('DELETE FROM project_action_routes WHERE project_id=? AND family=?', (identifier, family))
            else:
                store.db.execute('''INSERT INTO project_action_routes(project_id,family,route_id) VALUES(?,?,?)
                    ON CONFLICT(project_id,family) DO UPDATE SET route_id=excluded.route_id,version=project_action_routes.version+1''',
                    (identifier, family, body.route_id))
            store.db.execute('UPDATE projects SET routing_version=routing_version+1 WHERE id=?', (identifier,))
            changed(principal, 'routing.project_route_saved', identifier)
        return {'ok': True, 'routing_version': row['routing_version'] + 1}

    def save_execution(request, body, identifier=None):
        principal = admin(request, True)
        with store.transaction():
            if identifier:
                row = project(identifier, principal)
                if not row['routing_managed']:
                    raise DevError('PROJECT_ROUTING_REQUIRED', 'Adopt this project into shared-source routing first', 409)
                table, key, value = 'project_execution_settings', 'project_id', identifier
            else:
                table, key, value = 'space_execution_settings', 'space_id', principal.space_id
            old = store.one(f'SELECT * FROM {table} WHERE {key}=?', (value,))
            version(old, body.expected_version)
            store.db.execute(f'''INSERT INTO {table}({key},settings) VALUES(?,?) ON CONFLICT({key})
                DO UPDATE SET settings=excluded.settings,version={table}.version+1''', (value, encoded(body.settings.model_dump())))
            changed(principal, 'routing.execution_settings_saved', value)
            result = store.one(f'SELECT * FROM {table} WHERE {key}=?', (value,))
            return {**result, 'settings': json.loads(result['settings'])}

    @router.put('/api/project-routing/execution')
    @database_endpoint(store)
    def space_execution(request: Request, body: SettingsInput):
        return save_execution(request, body)

    @router.put('/api/projects/{identifier}/execution')
    @database_endpoint(store)
    def project_execution(identifier: str, request: Request, body: SettingsInput):
        return save_execution(request, body, identifier)

    @router.post('/api/project-routing/operations/{identifier}/release-source')
    @database_endpoint(store)
    def release_source(identifier: str, request: Request, body: ReleaseHold):
        principal = admin(request, True)
        with store.transaction():
            row = store.one('SELECT * FROM operations WHERE id=? AND space_id=?', (identifier, principal.space_id))
            if not row or row['state'] != body.expected_state or not row['route_context']:
                raise DevError('SOURCE_HOLD_CHANGED', 'Only an uncertain terminal routed operation can be explicitly released', 409)
            store.db.execute('UPDATE operations SET source_lock_released=1 WHERE id=?', (identifier,))
            changed(principal, 'routing.source_hold_released', identifier)
            runtime.wake_delivery()
        return {'ok': True, 'operation_state_unchanged': True, 'remote_termination_verified_by': 'administrator'}

    return router
