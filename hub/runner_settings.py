"""Panel-owner settings operations, independent of workspace/MCP privileges."""
from __future__ import annotations

import json
from dataclasses import replace
import time
import uuid

from fastapi import APIRouter, Request

from hub import iam
from hub.builtin_agent import is_builtin_device, require_external_device
from shared.runner_settings import RunnerSettingsRequest, SETTINGS_TOOL, SETTINGS_PROTOCOL
from shared.util import DevError


def require_settings_access(runtime, principal, device_id, *, online=False):
    principal = iam.live_principal(runtime.store, principal)
    if principal.grant_id or not principal.actor.startswith('panel:'):
        raise DevError('PANEL_REQUIRED', 'Runner settings require a device-owning panel account', 403)
    device = iam.require_device(runtime.store, replace(principal, space_id=None), device_id, manage=True)
    require_external_device(runtime.store, device_id)
    if online and (not device['enabled'] or not runtime.online(device_id)):
        raise DevError('DEVICE_OFFLINE', 'Runner settings were not queued while the device is offline or disabled', 409)
    return principal


def settings_project(runtime, principal, device_id):
    require_settings_access(runtime, principal, device_id, online=True)
    return {'id': None, 'alias': '', 'root': '', 'mode': 'read', 'allow_tasks': False,
            'device_id': device_id}


async def dispatch_settings(runtime, principal, device_id, body):
    project = await runtime.store.run(settings_project, runtime, principal, device_id)
    return await runtime.dispatch(SETTINGS_TOOL, body.model_dump(), project, principal)


def cache_key(device_id, kind):
    return 'runner-settings:' + device_id + ':' + kind


def cached(runtime, principal, device_id, kind):
    require_settings_access(runtime, principal, device_id)
    row = runtime.store.one('SELECT value FROM meta WHERE key=?', (cache_key(device_id, kind),))
    if not row:
        return {'protocol': SETTINGS_PROTOCOL, 'status': 'unavailable', 'stale': True,
                'observed_at': None, 'editable': False, 'hosts': []} if kind == 'discover' else {
                    'protocol': SETTINGS_PROTOCOL, 'status': 'unavailable', 'stale': True,
                    'observed_at': None, 'editable': False, 'configuration': {}, 'groups': []}
    value = json.loads(row['value'])
    return {**value, 'stale': True, 'editable': False,
            'status': 'offline' if not runtime.online(device_id) else 'cached'}


def remember(runtime, op, result):
    if op['tool'] != SETTINGS_TOOL or not result.get('ok') or not isinstance(result.get('data'), dict):
        return
    kind = json.loads(op.get('args_summary') or '{}').get('operation')
    if kind not in {'snapshot', 'discover', 'probe'}:
        return
    data = result['data']
    if len(json.dumps(data).encode()) > 2 * 1024 * 1024:
        return
    data = {**data, 'observed_at': time.time(), 'source_operation_id': op['id']}
    if kind == 'probe':
        alias = data.get('host_alias')
        if not isinstance(alias, str) or len(alias) > 253:
            return
        kind += ':' + alias
    runtime.store.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (cache_key(op['device_id'], kind), json.dumps(data)))
    runtime.store.after_commit(lambda: runtime.publish('device', {'id': op['device_id']}))


def approved_host(runtime, principal, device_id, alias):
    """Resource creation consumes a recent explicit scan, never untrusted form metadata."""
    require_settings_access(runtime, principal, device_id, online=True)
    row = runtime.store.one('SELECT value FROM meta WHERE key=?', (cache_key(device_id, 'discover'),))
    value = json.loads(row['value']) if row else {}
    if time.time() - value.get('observed_at', 0) > 300:
        raise DevError('SSH_DISCOVERY_REQUIRED', 'Refresh this Runner SSH inventory before saving a VPS resource', 409)
    if value.get('ssh_enabled') is not True:
        raise DevError('SSH_DISABLED', 'The selected Runner owner has not enabled SSH', 409)
    matches = [host for host in value.get('hosts', []) if host.get('host_alias') == alias]
    if len(matches) != 1 or matches[0].get('owner_approved') is not True or matches[0].get('status') != 'configured':
        raise DevError('SSH_ALIAS_NOT_APPROVED', 'The exact alias must be uniquely approved in this Runner SSH settings', 409)
    host = matches[0]
    import re
    if (not re.fullmatch('[A-Za-z0-9_-]{1,100}', host.get('connection_ref', ''))
            or not re.fullmatch('[a-f0-9]{64}', host.get('target_fingerprint', ''))):
        raise DevError('SSH_DISCOVERY_INVALID', 'Runner returned an invalid SSH target identity', 409)
    return host


def make_runner_settings_router(auth, runtime):
    router = APIRouter()
    store = runtime.store

    async def read_status(device_id, request, kind):
        def authorize():
            principal = auth.panel(request)
            iam.require_device(store, principal, device_id, manage=True)
            if is_builtin_device(store, device_id):
                return principal, True
            require_settings_access(runtime, principal, device_id)
            return principal, False
        principal, builtin = await store.run(authorize)
        if builtin:
            return {'protocol': SETTINGS_PROTOCOL, 'status': 'builtin_roots_only', 'editable': False,
                    'groups': [], 'configuration': {}, 'hosts': [],
                    'roots_url': '/api/devices/' + device_id + '/roots'}
        if not await store.run(runtime.online, device_id):
            return await store.run(cached, runtime, principal, device_id, kind)
        body = RunnerSettingsRequest(operation=kind, idempotency_key='settings-read-' + uuid.uuid4().hex)
        try:
            return await dispatch_settings(runtime, principal, device_id, body)
        except DevError as exc:
            if exc.code != 'DEVICE_OFFLINE':
                raise
            # A configuration reload reconnects to advertise capabilities. The
            # Runner may disconnect between the online check and admission.
            # Return a truthful stale observation; never retry a mutation.
            return await store.run(cached, runtime, principal, device_id, kind)

    @router.get('/api/devices/{device_id}/settings')
    async def get_settings(device_id: str, request: Request):
        return await read_status(device_id, request, 'snapshot')

    @router.post('/api/devices/{device_id}/settings')
    async def change_settings(device_id: str, request: Request, body: RunnerSettingsRequest):
        principal = await store.run(auth.panel, request, True)
        return await dispatch_settings(runtime, principal, device_id, body)

    @router.get('/api/devices/{device_id}/ssh-hosts')
    async def ssh_hosts(device_id: str, request: Request):
        return await read_status(device_id, request, 'discover')

    return router
