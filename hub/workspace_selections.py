"""Atomic, revision-checked workspace selections over persisted parent resources.

No SSH discovery/probe or Runner configuration is performed at this layer.
Deselection revokes use but retains identity for original session/receipt cleanup.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import uuid

from fastapi import APIRouter, Request
from pydantic import Field, model_validator

from hub import iam
from hub.db_worker import database_endpoint
from hub.workspace_mutations import label_key, mutate
from hub.workspace_resources import owned_resource
from hub.workspace_sources import fingerprint
from shared.workspace_contracts import WorkspaceModel, VPSBindingInput, identifiers
from shared.util import DevError


class VPSSelection(WorkspaceModel):
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=8, max_length=128)
    items: list[VPSBindingInput] = Field(max_length=500)

    @model_validator(mode='after')
    def unique(self):
        identifiers([item.resource_id for item in self.items])
        if any(not item.enabled for item in self.items):
            raise ValueError('Only selected VPS resources belong in items; omit deselected resources')
        names = [label_key(item.name)[1] for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError('Workspace VPS aliases must be distinct')
        return self


class DeviceChoice(WorkspaceModel):
    device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    alias: str = Field(default='', max_length=100)


class DeviceSelection(WorkspaceModel):
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=8, max_length=128)
    items: list[DeviceChoice] = Field(max_length=500)

    @model_validator(mode='after')
    def unique(self):
        identifiers([item.device_id for item in self.items])
        return self


def selection_revision(store, space_id, kind):
    if kind == 'vps':
        rows = store.all('SELECT * FROM space_vps WHERE space_id=? ORDER BY id', (space_id,))
        projects = store.all('SELECT * FROM vps_projects WHERE space_id=? ORDER BY vps_id,project_id', (space_id,))
        return fingerprint([rows, projects])
    return fingerprint(store.all('SELECT * FROM space_devices WHERE space_id=? ORDER BY device_id', (space_id,)))


def vps_snapshot(runtime, principal):
    store = runtime.store
    principal = iam.require_space_capability(store, principal, 'resources')
    rows = runtime.workspace_resources.inventory(replace(principal, space_id=None), 'vps_connections')
    bindings = {r['resource_id']: r for r in store.all('SELECT * FROM space_vps WHERE space_id=?', (principal.space_id,))}
    items = []
    for row in rows:
        binding = bindings.get(row['id'])
        item = {'resource': runtime.vps.inventory_public(row), 'selected': bool(binding and binding['enabled']), 'binding': None}
        if binding:
            item['binding'] = runtime.vps.public(runtime.vps.row(binding['id'], principal.space_id), principal, management=True)
        items.append(item)
    available = {row['id'] for row in rows}
    # A vanished ownership grant is reported, not replaced by another resource.
    unavailable = [{'id': b['id'], 'name': b['name'], 'selected': bool(b['enabled']), 'status': 'parent_unavailable'}
                   for rid, b in bindings.items() if rid not in available and b['enabled']]
    return {'items': items, 'unavailable': unavailable, 'revision': selection_revision(store, principal.space_id, 'vps')}


def save_vps(runtime, principal, body):
    store = runtime.store
    def authorize(current, receipt=None):
        iam.require_space_capability(store, current, 'resources')
        if receipt is not None:
            for entry in receipt.get('items', []):
                resource=owned_resource(store, replace(current,space_id=None), 'vps_connections', entry['resource']['id'])
                owned_resource(store, replace(current,space_id=None), 'devices', resource['runner_device_id'])
        for item in body.items:
            resource = owned_resource(store, replace(current, space_id=None), 'vps_connections', item.resource_id)
            owned_resource(store, replace(current, space_id=None), 'devices', resource['runner_device_id'])
            for project in item.projects:
                iam.project_in_space(store, current, project)
    def write(current):
        authorize(current)
        if selection_revision(store, current.space_id, 'vps') != body.expected_revision:
            raise DevError('VERSION_CHANGED', 'Workspace VPS selection changed; reload before saving', 409)
        old = {r['resource_id']: r for r in store.all('SELECT * FROM space_vps WHERE space_id=?', (current.space_id,))}
        chosen = {item.resource_id for item in body.items}
        # Temporary keys allow atomic alias swaps without weakening uniqueness.
        for row in old.values():
            store.db.execute('UPDATE space_vps SET name_key=? WHERE id=?', ('selection-' + uuid.uuid4().hex, row['id']))
        from hub.vps import VPSBindingSave
        for item in body.items:
            previous = old.get(item.resource_id)
            key = hashlib.sha256((body.idempotency_key + ':' + item.resource_id).encode()).hexdigest()
            runtime.vps.bind(VPSBindingSave(**item.model_dump(), expected_version=previous['version'] if previous else 0,
                                           idempotency_key=key), current, previous['id'] if previous else None)
        for resource_id, row in old.items():
            if resource_id not in chosen:
                store.db.execute('UPDATE space_vps SET enabled=0,name_key=?,version=version+? WHERE id=?',
                                 ('unassigned-' + row['id'], int(bool(row['enabled'])), row['id']))
        runtime.workspace_resources.changed(current, current.space_id)
        return vps_snapshot(runtime, current)
    return mutate(store, principal, 'vps.selection', body.idempotency_key, body.model_dump(), write, authorize_replay=authorize)


def device_snapshot(runtime, principal):
    store = runtime.store
    principal = iam.require_space_capability(store, principal, 'resources')
    from hub.api.devices import device_rows
    rows = device_rows(store, runtime, replace(principal, space_id=None))
    selected = {r['device_id']: r for r in store.all('SELECT * FROM space_devices WHERE space_id=?', (principal.space_id,))}
    return {'revision': selection_revision(store, principal.space_id, 'devices'), 'items': [
        {'device': {'id': row['id'], 'name': row['name'], 'online': row['online'], 'enabled': bool(row['enabled']), 'builtin': row['builtin']},
         'selected': bool(selected.get(row['id'], {}).get('enabled')),
         'alias': selected.get(row['id'], {}).get('alias', '')}
        for row in rows]}


def save_devices(runtime, principal, body):
    store = runtime.store
    def authorize(current, receipt=None):
        iam.require_space_capability(store, current, 'resources')
        if receipt is not None:
            for entry in receipt.get('items', []):
                owned_resource(store, replace(current,space_id=None), 'devices', entry['device']['id'])
        for item in body.items:
            owned_resource(store, replace(current, space_id=None), 'devices', item.device_id)
    def write(current):
        authorize(current)
        if selection_revision(store, current.space_id, 'devices') != body.expected_revision:
            raise DevError('VERSION_CHANGED', 'Workspace Device selection changed; reload before saving', 409)
        chosen = {item.device_id for item in body.items}
        for row in store.all('SELECT * FROM space_devices WHERE space_id=?', (current.space_id,)):
            if row['device_id'] not in chosen and row['enabled']:
                store.db.execute('UPDATE space_devices SET enabled=0,version=version+1 WHERE space_id=? AND device_id=?',
                                 (current.space_id, row['device_id']))
        for item in body.items:
            alias = label_key(item.alias)[0] if item.alias.strip() else ''
            store.db.execute('''INSERT INTO space_devices(space_id,device_id,alias,enabled) VALUES(?,?,?,1)
                ON CONFLICT(space_id,device_id) DO UPDATE SET alias=excluded.alias,enabled=1,version=space_devices.version+1''',
                             (current.space_id, item.device_id, alias))
        runtime.workspace_resources.changed(current, current.space_id)
        return device_snapshot(runtime, current)
    return mutate(store, principal, 'device.selection', body.idempotency_key, body.model_dump(), write, authorize_replay=authorize)


def make_selection_router(auth, runtime):
    router, store = APIRouter(), runtime.store
    @router.get('/api/spaces/{space_id}/vps-selection')
    @database_endpoint(store)
    def get_vps(request: Request):
        return vps_snapshot(runtime, auth.panel(request))
    @router.put('/api/spaces/{space_id}/vps-selection')
    @database_endpoint(store)
    def put_vps(body: VPSSelection, request: Request):
        return save_vps(runtime, auth.panel(request, True), body)
    @router.get('/api/spaces/{space_id}/device-selection')
    @database_endpoint(store)
    def get_devices(request: Request):
        return device_snapshot(runtime, auth.panel(request))
    @router.put('/api/spaces/{space_id}/device-selection')
    @database_endpoint(store)
    def put_devices(body: DeviceSelection, request: Request):
        return save_devices(runtime, auth.panel(request, True), body)
    return router
