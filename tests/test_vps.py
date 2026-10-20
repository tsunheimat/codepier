"""Whole-VPS workspace bindings, bounded consent and original-operation recovery.

No Role helpers, project-owned SSH password or legacy database fixture is used.
Display associations never authorize another target or impose a local root.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import shlex
import sqlite3
import time
import uuid

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from hub.runtime import Runtime
from hub.vps import VPSBindingSave, VPSInput
from shared.util import DevError
from tests.test_workspace_http import web  # noqa: F401
from tests.workspace_fixtures import WorkspaceFixture
from tests.workspace_stack import workspace_stack
from tests.support import wait_for


def input_body(**changes):
    return {'name': '测试 VPS', 'host': 'fixture.invalid', 'idempotency_key': uuid.uuid4().hex, **changes}


def binding_body(resource_id, **changes):
    return {'resource_id': resource_id, 'name': 'Workspace VPS', 'runner_device_id': 'a',
        'connection_ref': 'fixture', 'display_mode': 'top_level', 'projects': [],
        'idempotency_key': uuid.uuid4().hex, **changes}


def must(response):
    assert response.is_success, response.text
    return response.json()


def issue(client, binding, actions=('read', 'execute'), *, live=False):
    profile = must(client.post('/api/spaces/alice/profiles', json={
        'label': 'VPS '+uuid.uuid4().hex, 'idempotency_key': uuid.uuid4().hex,
        'policy': {'vps_rules': [{'vps': [binding['id']], 'actions': list(actions)}]}}))
    connection = must(client.post('/api/spaces/alice/connections', json={
        'name': 'Fixture', 'profile_id': profile['id'], 'profile_version': profile['version'],
        'consent_mode': 'live' if live else 'fixed', 'confirm_live_profile': live,
        'idempotency_key': uuid.uuid4().hex}))
    return profile, connection


def token_principal(app, connection):
    request = Request({'type': 'http', 'method': 'POST', 'path': '/mcp', 'query_string': b'',
        'headers': [(b'authorization', ('Bearer '+connection['token']).encode())]})
    return app.state.auth.bearer(request)


@pytest.fixture
def inventory(tmp_path):
    fixture = WorkspaceFixture(tmp_path/'data')
    runtime, owner = Runtime(fixture.store), fixture.principal
    resource = runtime.vps.save(VPSInput(**input_body()), replace(owner, space_id=None))
    binding = runtime.vps.bind(VPSBindingSave(**binding_body(resource['id'])), owner)
    try:
        yield runtime, owner, resource, binding
    finally:
        fixture.close()


@pytest.mark.parametrize('field', ['password', 'private_key', 'username', 'port', 'execution_project_id', 'project_ids'])
def test_inventory_rejects_removed_credential_and_project_route_inputs(field):
    with pytest.raises(ValidationError):
        VPSInput(**input_body(**{field: 'not-a-credential'}))


@pytest.mark.parametrize('mode,projects', [('project_only', []), ('both', []), ('top_level', ['project']), ('arbitrary', [])])
def test_visibility_has_exactly_three_unambiguous_modes(mode, projects):
    with pytest.raises(ValidationError):
        VPSBindingSave(**binding_body('server', display_mode=mode, projects=projects))


def test_http_inventory_idempotency_versions_csrf_and_bound_delete(web):
    client, app = web
    body = input_body()
    first = must(client.post('/api/vps', json=body))
    assert must(client.post('/api/vps', json=body))['id'] == first['id']
    update = input_body(name='Edited', expected_version=first['version'])
    saved = must(client.put('/api/vps/'+first['id'], json=update))
    assert saved['version'] == first['version']+1
    stale = client.put('/api/vps/'+first['id'], json=input_body(name='Stale', expected_version=first['version']))
    assert stale.status_code == 409
    assert client.post('/api/vps', json=input_body(), headers={'X-RD-CSRF': 'wrong'}).status_code == 403
    assert client.post('/api/vps', json=input_body(), headers={'Origin': 'https://evil.invalid'}).status_code == 403
    binding = must(client.post('/api/spaces/alice/vps', json=binding_body(first['id'])))
    assert client.delete('/api/vps/'+first['id'], params={'expected_version': saved['version']}).status_code == 409
    removed = must(client.delete('/api/spaces/alice/vps/'+binding['id'], params={'expected_version': binding['version']}))
    assert removed['remote_server_deleted'] is False and removed['remote_processes_stopped'] is False
    assert app.state.store.one('SELECT id FROM vps_connections WHERE id=?', (first['id'],))
    assert must(client.delete('/api/vps/'+first['id'], params={'expected_version': saved['version']}))['remote_server_deleted'] is False


def test_inactive_launcher_cross_workspace_project_and_duplicate_resource_rejected_atomically(web):
    client, app = web
    resource = must(client.post('/api/vps', json=input_body()))
    store = app.state.store
    store.execute("UPDATE devices SET enabled=0 WHERE id='c'")
    for changes, status, code in [
        (dict(runner_device_id='c'), 409, 'SOURCE_DEVICE_UNAVAILABLE'),
        (dict(display_mode='both', projects=['project', 'missing']), 404, 'PROJECT_NOT_FOUND'),
    ]:
        response = client.post('/api/spaces/alice/vps', json=binding_body(resource['id'], **changes))
        assert response.status_code == status and response.json()['error']['code'] == code, response.text
        assert store.all('SELECT id FROM space_vps') == []
        assert store.all('SELECT vps_id FROM vps_projects') == []
    assert client.post('/api/spaces/bob/vps', json=binding_body(resource['id'])).status_code == 403
    first = must(client.post('/api/spaces/alice/vps', json=binding_body(resource['id'])))
    duplicate = client.post('/api/spaces/alice/vps', json=binding_body(resource['id'], name='duplicate'))
    assert duplicate.status_code == 409
    assert [r['id'] for r in store.all('SELECT id FROM space_vps')] == [first['id']]


def test_binding_identity_cannot_be_retargeted_to_another_inventory_vps(web):
    client, _ = web
    first = must(client.post('/api/vps', json=input_body()))
    second = must(client.post('/api/vps', json=input_body(name='Another')))
    binding = must(client.post('/api/spaces/alice/vps', json=binding_body(first['id'])))
    response = client.put('/api/spaces/alice/vps/'+binding['id'], json=binding_body(second['id'], expected_version=binding['version']))
    assert response.status_code == 409 and response.json()['error']['code'] == 'VPS_IDENTITY_IMMUTABLE'


def test_saved_runner_must_advertise_the_selected_connection_reference(web):
    client, app = web
    resource = must(client.post('/api/vps', json=input_body()))
    app.state.store.execute("UPDATE devices SET info=? WHERE id='a'", (json.dumps({'shell_enabled': True, 'ssh_targets': ['reviewed']}),))
    denied = client.post('/api/spaces/alice/vps', json=binding_body(resource['id']))
    assert denied.status_code == 409 and denied.json()['error']['code'] == 'SSH_REFERENCE_NOT_AVAILABLE'
    binding = must(client.post('/api/spaces/alice/vps', json=binding_body(resource['id'], connection_ref='reviewed')))
    assert binding['connection_ref'] == 'reviewed'


def test_read_only_profile_cannot_execute_or_discover_ungranted_vps(web):
    client, app = web
    first = must(client.post('/api/vps', json=input_body()))
    binding = must(client.post('/api/spaces/alice/vps', json=binding_body(first['id'])))
    second = must(client.post('/api/vps', json=input_body(name='Other inventory')))
    other = must(client.post('/api/spaces/alice/vps', json=binding_body(second['id'], name='Private other')))
    _, connection = issue(client, binding, actions=('read',))
    principal = token_principal(app, connection)
    result = app.state.runtime.vps.list({}, principal)
    assert [v['id'] for v in result['vps']] == [binding['id']]
    assert result['vps'][0]['can_execute'] is False
    for identifier in (binding['id'], other['id']):
        with pytest.raises(DevError) as failure:
            app.state.runtime.vps.execution_project({'target': 'vps:'+identifier}, principal)
        assert failure.value.code == 'VPS_NOT_FOUND'


def test_whole_vps_execution_needs_no_project_or_source_mapping(inventory):
    runtime, owner, _, binding = inventory
    runtime.store.execute("UPDATE space_sources SET enabled=0")
    target = runtime.vps.execution_project({'target': 'vps:'+binding['id']}, owner)
    assert target['id'] is None and target['root'] == ''
    assert target['device_id'] == 'a'
    with pytest.raises(DevError):
        runtime.vps.execution_project({'target': 'vps:'+binding['id'], 'execution_device_id': 'b'}, owner)


@pytest.mark.parametrize('mode', ['top_level', 'project_only', 'both'])
def test_discovery_modes_use_the_same_binding_without_project_execution_permission(inventory, mode):
    runtime, owner, resource, binding = inventory
    body = binding_body(resource['id'], display_mode=mode, projects=['project'] if mode != 'top_level' else [],
                        expected_version=binding['version'])
    binding = runtime.vps.bind(VPSBindingSave(**body), owner, binding['id'])
    top, project = runtime.vps.list({}, owner)['vps'], runtime.vps.list({'project': 'codepier'}, owner)['vps']
    assert bool(top) == (mode in {'top_level', 'both'})
    assert bool(project) == (mode in {'project_only', 'both'})
    assert {r['id'] for r in top+project} == {binding['id']}
    assert runtime.vps.execution_project({'target': 'vps:'+binding['id']}, owner)['id'] is None


@pytest.mark.parametrize('change', ['disable_binding', 'disable_device', 'change_runner', 'change_reference', 'remove_binding'])
def test_queued_transport_never_substitutes_another_resource(inventory, change):
    runtime, owner, _, binding = inventory
    selected = runtime.vps.execution_project({'target': 'vps:'+binding['id']}, owner)
    request = {'project': selected, 'args': {'target': 'vps:'+binding['id']}}
    statements = {
        'disable_binding': ('UPDATE space_vps SET enabled=0 WHERE id=?', (binding['id'],)),
        'disable_device': ("UPDATE devices SET enabled=0 WHERE id='a'", ()),
        'change_runner': ("UPDATE space_vps SET runner_device_id='b' WHERE id=?", (binding['id'],)),
        'change_reference': ("UPDATE space_vps SET connection_ref='another' WHERE id=?", (binding['id'],)),
        'remove_binding': ('DELETE FROM space_vps WHERE id=?', (binding['id'],)),
    }
    runtime.store.execute(*statements[change])
    assert runtime.vps.permission_error(request, None, owner)
    with pytest.raises(DevError):
        runtime.vps.transport(request, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('change', ['disable', 'reference', 'delete'])
async def test_dispatched_revoked_request_only_probes_original_receipt(inventory, monkeypatch, accepted, change):
    runtime, owner, resource, binding = inventory
    receipt = await runtime.invoke('exec', {'target': 'vps:'+binding['id'], 'command': 'true',
        'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex}, owner)
    identifier = receipt['operation_id']
    runtime.store.execute('UPDATE operations SET attempts=1,accepted_at=? WHERE id=?',
                          (time.time() if accepted else None, identifier))
    if change == 'delete': runtime.store.execute('DELETE FROM space_vps WHERE id=?', (binding['id'],))
    else:
        changes = {'enabled': False} if change == 'disable' else {'connection_ref': 'changed'}
        runtime.vps.bind(VPSBindingSave(**binding_body(resource['id'], expected_version=binding['version'], **changes)), owner, binding['id'])
    packets = []
    class Connection:
        journal_id = 'fixture-journal'
        unusable = False
        async def send(self, packet): packets.append(packet)
    runtime.connections['a'] = Connection()
    monkeypatch.setattr(runtime, 'online', lambda _: True)
    monkeypatch.setattr(runtime, 'connection_authorized', lambda *a: True)
    def forbid(*a, **kw): pytest.fail('A dispatched request resolved a new SSH connection')
    monkeypatch.setattr(runtime.vps, 'transport', forbid)
    await runtime.deliver(identifier)
    assert packets == [{'type': 'probe', 'id': identifier}]
    row = runtime.store.one('SELECT state,result,attempts FROM operations WHERE id=?', (identifier,))
    assert row['state'] == 'queued' and row['result'] is None and row['attempts'] == 1


def terminal(server, identifier):
    def done():
        row = must(server.client.get('/api/operations/'+identifier))
        return row if row['state'] not in {'queued', 'running', 'reconnecting', 'cancelling'} else None
    try:
        return wait_for(done, timeout=25)
    except AssertionError:
        # Diagnostics are from this disposable fixture, never a live Hub.
        print(server.client.get('/api/operations/'+identifier).text)
        print((server.directory/'agent.log').read_text()[-4000:])
        print((server.directory/'hub.log').read_text()[-2000:])
        raise


@pytest.mark.integration
@pytest.mark.parametrize('command,timeout', [('exit 7', 10), ('sleep 5', 1)])
def test_saved_ssh_remote_failure_and_timeout_are_not_success(tmp_path, command, timeout):
    with workspace_stack(tmp_path/'failure') as server:
        receipt = server.mcp('exec', {'target': 'vps:'+'a'*32, 'command': command, 'timeout_seconds': timeout,
            'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})
        row = terminal(server, receipt['operation_id'])
        assert row['state'] != 'succeeded'
        assert row['result']['data']['command_ok'] is False


@pytest.mark.integration
def test_offline_queue_connection_revocation_prevents_execution(tmp_path):
    with workspace_stack(tmp_path/'revocation') as server:
        server.agent.terminate(); server.agent.wait(timeout=10)
        wait_for(lambda: not next(x['online'] for x in server.client.get('/api/devices').json()['devices'] if x['id']=='a'))
        marker = server.directory/'revoked-command'
        receipt = server.mcp('exec', {'target': 'vps:'+'a'*32, 'command': 'touch '+shlex.quote(str(marker)),
            'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})
        must(server.client.post('/api/spaces/alice/connections/'+server.grant_id+'/revoke', json={}))
        server.start_agent()
        # Revocation also removes disclosure authority. Inspect this disposable
        # fixture's database, never restore a revoked token just to poll output.
        denied = server.client.get('/api/operations/'+receipt['operation_id'])
        assert denied.status_code == 403 and denied.json()['error']['code'] == 'ACCESS_DENIED'
        def failed_without_dispatch():
            with closing(sqlite3.connect(server.hubdir/'hub.sqlite3')) as db:
                row = db.execute('SELECT state,attempts FROM operations WHERE id=?', (receipt['operation_id'],)).fetchone()
            return row if row and row[0] == 'failed' else None
        assert wait_for(failed_without_dispatch, timeout=25) == ('failed', 0)
        assert not marker.exists()


@pytest.mark.parametrize('live', [False, True])
def test_display_metadata_does_not_change_consent_but_connection_changes_do(web, live):
    client, app = web
    resource = must(client.post('/api/vps', json=input_body()))
    binding = must(client.post('/api/spaces/alice/vps', json=binding_body(resource['id'])))
    _, connection = issue(client, binding, live=live)
    principal = token_principal(app, connection)
    runtime = app.state.runtime
    target = {'target': 'vps:'+binding['id']}
    original = runtime.vps.execution_project(target, principal)
    must(client.put('/api/vps/'+resource['id'], json=input_body(
        name='New display', host='metadata-only.invalid', expected_version=resource['version'])))
    changed = must(client.put('/api/spaces/alice/vps/'+binding['id'], json=binding_body(resource['id'],
        name='New binding label', display_mode='both', projects=['project'], expected_version=binding['version'])))
    assert runtime.vps.execution_project(target, principal)['_vps_context'] == original['_vps_context']
    must(client.put('/api/spaces/alice/vps/'+binding['id'], json=binding_body(resource['id'],
        connection_ref='another-approved-target', expected_version=changed['version'])))
    if live:
        assert runtime.vps.execution_project(target, principal)['_vps_context']['connection_ref'] == 'another-approved-target'
    else:
        with pytest.raises(DevError) as failure:
            runtime.vps.execution_project(target, principal)
        assert failure.value.code == 'CONNECTION_TARGET_CHANGED'
    assert token_principal(app, connection).grant_id == principal.grant_id


def test_same_connection_key_with_changed_input_is_not_a_second_creation(web):
    client, _ = web
    resource = must(client.post('/api/vps', json=input_body()))
    body = binding_body(resource['id'])
    first = must(client.post('/api/spaces/alice/vps', json=body))
    conflict = client.post('/api/spaces/alice/vps', json={**body, 'connection_ref': 'different'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert [r['id'] for r in must(client.get('/api/spaces/alice/vps'))['vps']] == [first['id']]


@pytest.mark.integration
def test_ssh_cancellation_stops_original_launcher_without_claiming_remote_certainty(tmp_path):
    with workspace_stack(tmp_path/'cancel') as server:
        started, completed = server.directory/'started', server.directory/'completed'
        values = {'target': 'vps:'+'a'*32, 'command': 'touch '+shlex.quote(str(started))+
            '; sleep 30; touch '+shlex.quote(str(completed)), 'timeout_seconds': 60,
            'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex}
        receipt = server.mcp('exec', values)
        identifier = receipt['operation_id']
        wait_for(started.exists, timeout=15)
        server.mcp('process', {'operation': 'cancel', 'operation_ids': [identifier]})
        row = terminal(server, identifier)
        # Stopping the original launcher is confirmed, but the remote side is
        # not; needs_review must take precedence over a local cancelled flag.
        assert row['state'] == 'needs_review'
        data = row['result']['data']
        assert data['cancelled'] and data['command_ok'] is False
        assert data['outcome_uncertain'] and data['remote_execution_uncertain']
        assert data['ssh_error'] == 'SSH_CANCELLED'
        assert server.mcp('exec', values, expect_error=True)['operation_id'] == identifier
        assert not completed.exists()
        repeated = server.mcp('process', {'operation': 'cancel', 'operation_ids': [identifier]}, expect_error=True)
        assert repeated['operations'] == [{'operation_id': identifier, 'state': 'needs_review', 'cancel_requested': False}]
        assert terminal(server, identifier)['state'] == 'needs_review'


@pytest.mark.integration
def test_profile_sharing_never_shares_original_vps_operation_receipts(tmp_path):
    with workspace_stack(tmp_path/'privacy') as server:
        profiles = must(server.client.get('/api/spaces/alice/profiles'))['profiles']
        profile = profiles[0]
        second = must(server.client.post('/api/spaces/alice/connections', json={
            'name': 'Separate identity', 'profile_id': profile['id'], 'profile_version': profile['version'],
            'consent_mode': 'fixed', 'idempotency_key': uuid.uuid4().hex}))
        receipt = server.mcp('exec', {'target': 'vps:'+'a'*32, 'command': 'printf private-vps-result',
            'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})
        assert server.finish(receipt)['output'] == 'private-vps-result'
        for operation in ['get', 'cancel']:
            response = must(server.client.post('/mcp', headers={'Authorization': 'Bearer '+second['token'],
                'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'},
                json={'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'tools/call',
                    'params': {'name': 'process', 'arguments': {'operation': operation, 'operation_ids': [receipt['operation_id']]}}}))
            assert response['result']['isError'] is True
            assert 'private-vps-result' not in json.dumps(response)
        original = server.mcp('process', {'operation': 'get', 'operation_ids': [receipt['operation_id']]})
        assert 'private-vps-result' in json.dumps(original)


def test_removing_independent_device_allocation_preserves_vps_launcher(inventory):
    runtime,owner,_,binding=inventory
    selected=runtime.vps.execution_project({'target':'vps:'+binding['id']},owner)
    request={'project':selected,'args':{'target':'vps:'+binding['id']}}
    runtime.store.execute("DELETE FROM space_devices WHERE device_id='a' AND space_id='alice'")
    assert runtime.vps.permission_error(request,None,owner) is None
    assert runtime.vps.transport(request,None)['project']['_ssh_target']['vps_id']==binding['id']
