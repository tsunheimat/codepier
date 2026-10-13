"""Trusted built-in identity, panel controls, and standalone lifecycle fences."""
import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from hub.agent_install import make_agent_install_router
from hub.api.context import HubContext
from hub.api.devices import make_devices_router
from hub.auth import Auth
from hub.builtin_agent import META_KEY, builtin_device_id, ensure_identity
from hub.runtime import Runtime
from hub.store import Store
from shared.crypto import digest, token
from shared.util import DevError
from tests.install_support import package_tree
from tests.legacy_iam_fixture import attach_session_security, seed_personal_owner


@pytest.fixture
def panel(tmp_path):
    store = Store(tmp_path / 'hub')
    space = seed_personal_owner(store, 'owner', 'admin')
    builtin = ensure_identity(store)['device_id']
    remote = 'remote-device'
    info = json.dumps({'builtin': True, 'version': 'old', 'roots': [],
                       'device_actions': ['agent_update', 'agent_restart', 'agent_uninstall'],
                       'management': {'managed': True, 'service': True, 'reason': 'agent claim'}})
    store.execute('UPDATE devices SET info=? WHERE id=?', (info, builtin))
    store.execute('INSERT INTO devices(id,name,secret,info,created,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?)',
                  (remote, 'Remote', store.encrypt('remote-secret'), info, 2, space, 'owner'))
    for user, cookie in [('owner', 'owner-cookie'), ('other', 'other-cookie')]:
        seed_personal_owner(store, user, 'admin' if user == 'owner' else user)
        store.execute('INSERT INTO sessions(id_hash,user_id,csrf,expires) VALUES(?,?,?,?)',
                      (digest(cookie), user, 'csrf', time.time() + 3600))
    attach_session_security(store)
    runtime = Runtime(store)
    runtime.online = lambda _: True
    wakes, disconnects = [], []

    def wake():
        # Observe committed state from the event loop, not the mutation transaction.
        wakes.append(store.one('SELECT enabled,secret FROM devices WHERE id=?', (builtin,)))

    async def disconnect(device_id, reason):
        disconnects.append((device_id, reason))

    runtime.builtin_agent = SimpleNamespace(wake=wake)
    runtime.disconnect_device = disconnect
    auth = Auth(store)
    app = FastAPI()

    @app.exception_handler(DevError)
    async def error(request, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)

    source = tmp_path / 'source'
    package_tree(source)
    app.include_router(make_devices_router(HubContext(store, runtime, auth, None, None,
                                                     lambda: 'https://hub.invalid', Path(source))))
    app.include_router(make_agent_install_router(runtime, auth, source))
    with TestClient(app) as client:
        client.cookies.set('rd_session', 'owner-cookie')
        client.headers['X-RD-CSRF'] = 'csrf'
        yield SimpleNamespace(client=client, store=store, runtime=runtime, auth=auth,
                              builtin=builtin, remote=remote, space=space,
                              wakes=wakes, disconnects=disconnects)
    store.close()


def principal(panel, cookie='owner-cookie'):
    request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                       'headers': [(b'cookie', ('rd_session=' + cookie).encode())]})
    return panel.auth.panel(request)


def assert_managed(response):
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'BUILTIN_AGENT_MANAGED'


def test_trusted_marker_suppresses_only_builtin_lifecycle(panel):
    rows = {row['id']: row for row in panel.client.get('/api/devices').json()['devices']}
    builtin, remote = rows[panel.builtin], rows[panel.remote]
    assert builtin['builtin'] is True and remote['builtin'] is False
    assert remote['info']['builtin'] is True  # Agent-reported spoof is not authority.
    assert builtin['agent']['actions'] == []
    assert all(builtin['agent'][key] is False for key in
               ('update_available', 'service', 'can_update', 'can_restart', 'can_uninstall'))
    assert 'Hub 部署管理' in builtin['agent']['reason']
    assert remote['agent']['can_update'] and remote['agent']['can_restart'] and remote['agent']['can_uninstall']
    panel.runtime.online = lambda _: False
    builtin = next(row for row in panel.client.get('/api/devices').json()['devices'] if row['builtin'])
    assert 'Hub 部署管理' in builtin['agent']['reason']


@pytest.mark.parametrize('route,body', [
    ('install-ticket', {'hub_url': 'https://hub.invalid', 'platform': 'posix', 'allow_root': '/projects'}),
    ('agent-commands', {'hub_url': 'https://hub.invalid', 'platform': 'posix'}),
    ('agent-update', {'idempotency_key': 'builtin-test-update'}),
    ('agent-restart', {'idempotency_key': 'builtin-test-restart'}),
    ('agent-uninstall', {'idempotency_key': 'builtin-test-uninstall', 'confirmation': 'wrong name'}),
])
def test_builtin_standalone_routes_rejected_after_authorization(panel, route, body):
    url = f'/api/devices/{panel.builtin}/{route}'
    assert_managed(panel.client.post(url, json=body))
    assert not panel.store.one('SELECT 1 FROM agent_install_tickets')
    assert not panel.store.one('SELECT 1 FROM operations')
    panel.client.cookies.set('rd_session', 'other-cookie')
    response = panel.client.post(url, json=body)
    assert response.status_code in (403, 404)
    assert response.json()['error']['code'] != 'BUILTIN_AGENT_MANAGED'
    panel.client.cookies.clear()
    assert panel.client.post(url, json=body).status_code == 401


def test_builtin_enrollment_ticket_cannot_export_credentials(panel):
    secret = panel.store.decrypt(panel.store.one('SELECT secret FROM devices WHERE id=?', (panel.builtin,))['secret'])
    ticket = 'rdi_' + token(32)
    panel.store.execute('INSERT INTO agent_install_tickets VALUES(?,?,?,?,?,?)',
                        (digest(ticket), panel.builtin, digest(secret), 'https://hub.invalid', time.time() + 900, time.time()))
    response = panel.client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + ticket})
    assert_managed(response)
    assert secret not in response.text
    assert panel.store.one('SELECT 1 FROM agent_install_tickets')
    # Invalid ticket authority must not disclose that the identity is built in.
    assert panel.client.post('/agent/enroll', headers={'Authorization': 'Bearer rdi_' + token(32)}).status_code == 401


def test_builtin_rotation_acknowledges_without_secret_and_wakes(panel):
    before = panel.store.one('SELECT secret FROM devices WHERE id=?', (panel.builtin,))['secret']
    response = panel.client.post(f'/api/devices/{panel.builtin}/rotate')
    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True, 'builtin': True}
    after = panel.store.one('SELECT secret FROM devices WHERE id=?', (panel.builtin,))['secret']
    assert panel.store.decrypt(before) != panel.store.decrypt(after)
    assert len(panel.wakes) == 1 and panel.wakes[0]['secret'] == after
    assert panel.disconnects == [(panel.builtin, 'Device key rotated')]
    assert panel.store.one("SELECT 1 FROM audit WHERE action='device.key_rotated' AND target=?", (panel.builtin,))


def test_builtin_enable_disable_rename_delete_and_tombstone(panel):
    url = f'/api/devices/{panel.builtin}'
    assert panel.client.patch(url, json={'name': 'Custom Hub'}).status_code == 200
    assert not panel.wakes
    assert panel.client.patch(url, json={'enabled': False}).status_code == 200
    assert panel.wakes[-1]['enabled'] == 0
    assert panel.disconnects[-1] == (panel.builtin, 'Disabled by owner')
    assert panel.client.patch(url, json={'enabled': True}).status_code == 200
    assert panel.wakes[-1]['enabled'] == 1
    assert panel.client.delete(url).status_code == 200
    assert panel.wakes[-1] is None and len(panel.wakes) == 3
    assert panel.disconnects[-1] == (panel.builtin, 'Device removed')
    assert builtin_device_id(panel.store) == panel.builtin
    assert ensure_identity(panel.store)['device_id'] == panel.builtin
    assert panel.store.one('SELECT id FROM devices WHERE id=?', (panel.builtin,)) is None


def test_rollback_does_not_wake_supervisor(panel, monkeypatch):
    audit = panel.store.audit

    def fail_audit(*args, **kwargs):
        if args[1] == 'device.updated':
            raise DevError('TEST_ROLLBACK', 'rollback', 409)
        return audit(*args, **kwargs)

    monkeypatch.setattr(panel.store, 'audit', fail_audit)
    response = panel.client.patch(f'/api/devices/{panel.builtin}', json={'enabled': False})
    assert response.status_code == 409
    assert not panel.wakes and not panel.disconnects
    assert panel.store.one('SELECT enabled FROM devices WHERE id=?', (panel.builtin,))['enabled'] == 1


@pytest.mark.parametrize('action', ['agent_update', 'agent_restart', 'agent_uninstall'])
@pytest.mark.parametrize('phase', ['resolution', 'admission', 'admission-with-project'])
def test_runtime_rejects_builtin_lifecycle_before_durable_admission(panel, action, phase):
    caller = principal(panel)
    args = {'idempotency_key': 'builtin-runtime-test'}
    project = {'id': 'spoofed-project' if phase == 'admission-with-project' else None,
               'device_id': panel.builtin, 'alias': '', 'root': '', 'mode': 'write', 'allow_tasks': False}
    with pytest.raises(DevError) as caught:
        if phase == 'resolution':
            asyncio.run(panel.runtime.dispatch_device_action(action, args, panel.builtin, caller))
        else:
            asyncio.run(panel.runtime.dispatch(action, args, project, caller, background=True))
    assert caught.value.code == 'BUILTIN_AGENT_MANAGED'
    assert not panel.store.one('SELECT 1 FROM operations')
    other = principal(panel, 'other-cookie')
    with pytest.raises(DevError) as caught:
        panel.runtime._admit_operation(action, args, project, other)
    assert caught.value.code != 'BUILTIN_AGENT_MANAGED'


def test_other_space_cannot_list_rotate_or_mutate_builtin(panel):
    panel.client.cookies.set('rd_session', 'other-cookie')
    assert panel.client.get('/api/devices').json()['devices'] == []
    url = f'/api/devices/{panel.builtin}'
    assert panel.client.post(url + '/rotate').status_code in (403, 404)
    assert panel.client.patch(url, json={'enabled': False}).status_code in (403, 404)
    assert panel.client.delete(url).status_code in (403, 404)
    assert not panel.wakes


def test_remote_pairing_install_and_runtime_lifecycle_unchanged(panel):
    url = f'/api/devices/{panel.remote}'
    response = panel.client.post(url + '/rotate')
    pairing = response.json()['pairing']
    assert pairing['device_id'] == panel.remote and pairing['secret']
    assert not panel.wakes and 'builtin' not in response.json()
    body = {'hub_url': 'https://hub.invalid', 'platform': 'posix', 'allow_root': '/projects'}
    response = panel.client.post(url + '/install-ticket', json=body)
    assert response.status_code == 200, response.text
    ticket = re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})', response.json()['command']).group(1)
    response = panel.client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + ticket})
    assert response.json()['secret'] == pairing['secret']
    assert panel.client.post(url + '/agent-commands', json={key: body[key] for key in ('hub_url', 'platform')}).status_code == 200
    response = panel.client.post(url + '/agent-restart', json={'idempotency_key': 'remote-test-restart'})
    assert response.status_code == 200, response.text
    assert response.json()['operation_id']


def test_invalid_marker_fails_closed(panel):
    panel.store.execute('UPDATE meta SET value=? WHERE key=?', ('not-json', META_KEY))
    response = panel.client.post(f'/api/devices/{panel.builtin}/agent-restart',
                                 json={'idempotency_key': 'invalid-marker-test'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'BUILTIN_AGENT_STATE'
    assert not panel.store.one('SELECT 1 FROM operations')
