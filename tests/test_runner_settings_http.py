"""Real HTTP authorization and live Hub/Runner settings round trips."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

import httpx
import pytest

from hub.runner_settings import cache_key, require_settings_access
from shared.util import DevError, atomic_json
from tests.test_workspace_http import web  # noqa: F401
from tests.workspace_stack import workspace_stack


def body(operation, **values):
    return {'operation': operation, 'idempotency_key': uuid.uuid4().hex, **values}


def test_offline_settings_are_unavailable_and_mutations_never_queue(web):
    client, app = web
    snapshot = client.get('/api/devices/a/settings')
    assert snapshot.status_code == 200 and snapshot.json()['status'] == 'unavailable'
    assert snapshot.json()['editable'] is False
    response = client.post('/api/devices/a/settings', json=body('apply', expected_revision='a'*64, patch={'name':'offline'}))
    assert response.status_code == 409 and response.json()['error']['code'] == 'DEVICE_OFFLINE'
    assert app.state.store.all("SELECT id FROM operations WHERE tool='runner_settings'") == []


def test_offline_cache_never_claims_freshness_and_is_owner_protected(web):
    client, app = web
    app.state.store.execute('INSERT INTO meta(key,value) VALUES(?,?)',
        (cache_key('a','snapshot'), json.dumps({'editable':True, 'configuration':{'name':'owner-only'},
                                            'observed_at':1, 'revision':'a'*64})))
    cached = client.get('/api/devices/a/settings').json()
    assert cached['configuration']['name'] == 'owner-only'
    assert cached['stale'] and not cached['editable'] and cached['status'] == 'offline'
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    app.state.store.execute("UPDATE devices SET owner_user_id='bob' WHERE id='a'")
    assert client.get('/api/devices/a/settings').status_code == 403
    assert client.get('/api/devices/a/ssh-hosts').status_code == 403


def test_settings_write_requires_csrf_and_strict_contract(web):
    client, app = web
    invalid = client.post('/api/devices/a/settings', json=body('apply', expected_revision='a'*64, patch={}, device_id='other'))
    assert invalid.status_code == 422
    headers = dict(client.headers)
    client.headers.pop('x-rd-csrf')
    try:
        result = client.post('/api/devices/a/settings', json=body('snapshot'))
        assert result.status_code == 403
    finally:
        client.headers.update(headers)
    assert app.state.store.all("SELECT id FROM operations WHERE tool='runner_settings'") == []


def test_mcp_cannot_acquire_settings_access_via_workspace_device_permissions(web):
    from hub import iam
    client, app = web
    user = app.state.store.one("SELECT * FROM users WHERE id='alice'")
    assert user
    from hub.principal import Principal
    principal = iam.live_principal(app.state.store, Principal('panel:alice','alice',{'read'},[],user_epoch=1))
    with pytest.raises(DevError) as denied:
        require_settings_access(app.state.runtime, replace(principal, actor='mcp:forged', grant_id='not-a-panel'), 'a')
    assert denied.value.code == 'INVALID_TOKEN'
    with pytest.raises(DevError) as origin:
        require_settings_access(app.state.runtime, replace(principal, actor='mcp:forged'), 'a')
    assert origin.value.code == 'PANEL_REQUIRED'
    from shared.contracts import TOOLS
    assert 'runner_settings' not in TOOLS


@pytest.mark.integration
@pytest.mark.parametrize('discovery_state', ['live', 'unavailable', 'cached'])
def test_real_runner_settings_default_denial_owner_opt_in_cas_and_no_session_interruption(tmp_path, monkeypatch, discovery_state):
    with workspace_stack(tmp_path/'settings-roundtrip') as server:
        client = server.client
        def snapshot():
            return server.runner_read('settings')
        def submit(operation, **values):
            return server.must(client.post('/api/devices/a/settings', json=body(operation, **values)))
        current = snapshot()
        assert not current['editable'] and len(current['groups']) == 15 and not current.get('stale')
        assert server.config['secret'] not in json.dumps(current)
        revision = current['revision']
        preview = server.finish(submit('preview', expected_revision=revision, patch={'name':'reviewed'}))
        assert preview['validated'] and not preview['saved']
        denied = client.post('/api/devices/a/settings', json=body('apply', expected_revision=revision, patch={'name':'forbidden'}))
        assert denied.status_code == 409 and denied.json()['error']['code'] == 'CONFIG_OWNER_APPROVAL_REQUIRED'
        identifier = denied.json()['error']['operation_id']
        for _ in range(200):
            receipt = server.must(client.get('/api/operations/'+identifier))
            if receipt['state'] not in {'queued','running','reconnecting'}:
                break
            time.sleep(.05)
        assert receipt['result']['error']['code'] == 'CONFIG_OWNER_APPROVAL_REQUIRED'
        assert json.loads(server.config_path.read_text())['name'] == 'Runner a'
        # Only the isolated fixture's local owner changes its opt-in. No live
        # deployment config is used, and no remote execution bypass is offered.
        local = json.loads(server.config_path.read_text())
        local['settings_management'] = {'enabled':True}
        atomic_json(server.config_path, local)
        for _ in range(100):
            current = snapshot()
            if current['editable']:
                break
            time.sleep(.1)
        assert current['editable']
        session = server.execute('session', operation='open', project='codepier', mode='persistent_shell')
        session_id = session['session_id']
        try:
            changed = server.finish(submit('apply', expected_revision=current['revision'],
                patch={'name':'Saved via owner panel', 'sessions':{'enabled':True,'idle_seconds':30,'lifetime_seconds':30}}))
            assert changed['saved'] and not changed['applied'] and changed['backup_revision'] == current['revision']
            assert not changed['service_restarted']
            for _ in range(100):
                live = snapshot()
                if not live.get('stale') and live['effective']['name'] == 'Saved via owner panel':
                    break
                time.sleep(.1)
            assert live['configuration']['name'] == live['effective']['name'] == 'Saved via owner panel'
            executed = server.execute('session', operation='exec', session_id=session_id, command='printf retained-session')
            assert executed['stdout'] == 'retained-session'
            rollback = server.finish(submit('rollback', expected_revision=live['revision'], backup_revision=changed['backup_revision']))
            assert rollback['saved'] and json.loads(server.config_path.read_text())['name'] == 'Runner a'
            # Persistence is not live application. Observe this exact rollback
            # revision on the same Runner before continuing the acceptance flow.
            for _ in range(100):
                restored = snapshot()
                if (restored['revision'] == rollback['revision']
                        and restored['effective']['name'] == 'Runner a'
                        and not restored['pending_fields']):
                    break
                time.sleep(.1)
            assert restored['revision'] == rollback['revision']
            assert restored['effective']['name'] == 'Runner a'
            assert not restored['pending_fields']
        finally:
            server.execute('session', operation='close', session_id=session_id)
        # Deterministically exercise the documented read response during the
        # rollback/re-advertisement gap. The next read still uses the real Hub
        # and Runner; no mutation or execution receipt is submitted twice.
        original_get = client.get
        discovery_reads = []
        def observe_get(url, **kwargs):
            if url == '/api/devices/a/ssh-hosts':
                discovery_reads.append(url)
                if discovery_state != 'live' and len(discovery_reads) == 1:
                    hosts = [] if discovery_state == 'unavailable' else [
                        {'host_alias':'fixture', 'discovered':True, 'owner_approved':True,
                         'status':'configured', 'tested':False}]
                    return httpx.Response(200, json={
                        'protocol':current['protocol'], 'status':discovery_state,
                        'stale':True, 'editable':False, 'observed_at':None if not hosts else 1,
                        'hosts':hosts})
            return original_get(url, **kwargs)
        monkeypatch.setattr(client, 'get', observe_get)
        discovered = server.runner_read('ssh-hosts')
        fixture = next(host for host in discovered['hosts'] if host['host_alias']=='fixture')
        assert not discovered.get('stale'), discovered
        if discovery_state != 'live':
            assert len(discovery_reads) >= 2
        assert fixture['owner_approved'] and fixture['discovered'] and fixture['status']=='configured'
        assert not fixture['tested']
        # A display-only edit is independent of the Space's immutable target.
        resource = server.must(client.get('/api/vps'))['vps'][0]
        saved = server.must(client.put('/api/vps/'+resource['id'], json={
            'name':resource['name'], 'runner_device_id':'a','host_alias':'fixture','description':'verified metadata flow',
            'expected_version':resource['version'],'idempotency_key':uuid.uuid4().hex}))
        assert saved['description']=='verified metadata flow'


@pytest.mark.parametrize('has_cache', [False, True])
@pytest.mark.parametrize('route,kind',[('settings','snapshot'),('ssh-hosts','discover')])
def test_read_reconnect_race_returns_stale_observation_without_replaying(web, monkeypatch, route, kind, has_cache):
    client, app = web
    value = {'configuration':{'name':'last observed'}, 'editable':True, 'observed_at':1,
             'hosts':[], 'ssh_enabled':True}
    if has_cache:
        app.state.store.execute('INSERT INTO meta(key,value) VALUES(?,?)', (cache_key('a',kind),json.dumps(value)))
    monkeypatch.setattr(app.state.runtime,'online',lambda device:True)
    calls=[]
    async def disconnected(*args):
        calls.append(args)
        raise DevError('DEVICE_OFFLINE','Disconnected during capability advertisement',409)
    monkeypatch.setattr('hub.runner_settings.dispatch_settings',disconnected)
    response=client.get('/api/devices/a/'+route)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['stale'] and not result['editable']
    assert result['observed_at'] == (1 if has_cache else None)
    assert result['status'] == ('cached' if has_cache else 'unavailable')
    if kind == 'discover':
        assert result['hosts'] == []
    assert len(calls)==1 and not app.state.store.all("SELECT id FROM operations WHERE tool='runner_settings'")


def test_read_reconnect_handling_does_not_hide_permission_denial(web, monkeypatch):
    client,app=web
    monkeypatch.setattr(app.state.runtime,'online',lambda device:True)
    async def denied(*args):
        raise DevError('PANEL_REQUIRED','No longer authorized',403)
    monkeypatch.setattr('hub.runner_settings.dispatch_settings',denied)
    response=client.get('/api/devices/a/settings')
    assert response.status_code==403 and response.json()['error']['code']=='PANEL_REQUIRED'
