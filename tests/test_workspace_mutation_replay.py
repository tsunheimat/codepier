"""Configuration receipt recovery rechecks live authority without repeating writes."""
import json
import sqlite3
import uuid

import pytest

from tests.test_workspace_http import web as base_web  # noqa: F401
from tests.vps_fixtures import seed_discovery


@pytest.fixture
def web(base_web, monkeypatch):
    client, app = base_web
    seed_discovery(app.state.runtime)
    original = app.state.runtime.online
    monkeypatch.setattr(app.state.runtime, "online", lambda device: device == "a" or original(device))
    return client, app


def pool_body():
    return {'name': 'Reviewed pool', 'abstract_base': '/storage/shared',
            'default_fs_device_id': 'b', 'default_shell_device_id': 'a',
            'expected_version': 1, 'idempotency_key': uuid.uuid4().hex}


def shell_body():
    return {'settings': {'mode': 'inherit'}, 'expected_version': 0,
            'idempotency_key': uuid.uuid4().hex}


def restrict_management(store, space='alice'):
    store.execute('UPDATE memberships SET policy=? WHERE space_id=? AND user_id=?',
                  (json.dumps({'project_rules': [{'all_projects': True, 'actions': ['read']}]}), space, 'alice'))


def test_pool_update_replay_after_ownership_revocation(web):
    client, app = web
    store = app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    body = pool_body()
    first = client.put('/api/storage-pools/pool', json=body)
    assert first.status_code == 200, first.text
    store.execute("UPDATE storage_pools SET owner_user_id='bob' WHERE id='pool'")
    before = store.one("SELECT * FROM storage_pools WHERE id='pool'")
    hidden = client.get('/api/storage-pools/pool/mappings')
    fresh = client.put('/api/storage-pools/pool', json={**body, 'idempotency_key': uuid.uuid4().hex})
    replay = client.put('/api/storage-pools/pool', json=body)
    assert hidden.status_code == fresh.status_code == 404
    assert replay.status_code == 404, 'Revoked ownership must deny receipt disclosure'
    assert store.one("SELECT * FROM storage_pools WHERE id='pool'") == before


def test_project_shell_replay_after_management_revocation(web):
    client, app = web
    store = app.state.store
    body = shell_body()
    first = client.put('/api/projects/project/shell', json=body)
    assert first.status_code == 200, first.text
    restrict_management(store)
    assert client.get('/api/projects/project').status_code == 200
    before = store.one("SELECT * FROM project_shell_settings WHERE project_id='project'")
    hidden = client.get('/api/projects/project/shell')
    fresh = client.put('/api/projects/project/shell', json={**body, 'idempotency_key': uuid.uuid4().hex})
    replay = client.put('/api/projects/project/shell', json=body)
    assert hidden.status_code == fresh.status_code == 403
    assert replay.status_code == 403, 'Revoked management must deny receipt disclosure'
    assert store.one("SELECT * FROM project_shell_settings WHERE project_id='project'") == before


CASES = ('pool', 'shell', 'device', 'project_execution', 'space_execution',
         'vps_create', 'vps_update', 'vps_bind_create', 'vps_bind_update',
         'profile_create', 'profile_update', 'space_create', 'space_update', 'member')


def saved(response):
    assert response.is_success, response.text
    return response.json()


def mutation_request(client, case):
    """Exercise every mutate caller through its production HTTP endpoint."""
    body = {'idempotency_key': uuid.uuid4().hex}
    if case == 'pool':
        return 'PUT', '/api/storage-pools/pool', pool_body()
    if case == 'shell':
        return 'PUT', '/api/projects/project/shell', shell_body()
    if case == 'device':
        return 'PUT', '/api/spaces/alice/devices/a', {**body, 'enabled': True, 'expected_version': 1}
    if case.endswith('_execution'):
        prefix = '/api/projects/project' if case == 'project_execution' else '/api/spaces/alice'
        return 'PUT', prefix+'/execution', {**body, 'settings': {}, 'expected_version': 0}
    if case.startswith('vps_'):
        inventory = {**body, 'name': 'Receipt VPS', 'runner_device_id': 'a', 'host_alias': 'fixture'}
        if case == 'vps_create':
            return 'POST', '/api/vps', inventory
        resource = saved(client.post('/api/vps', json=inventory))
        if case == 'vps_update':
            return 'PUT', '/api/vps/'+resource['id'], {
                **inventory, 'name': 'Updated VPS', 'expected_version': resource['version'], 'idempotency_key': uuid.uuid4().hex}
        binding = {**body, 'resource_id': resource['id'], 'name': 'Receipt binding', 'display_mode': 'both', 'projects': ['project']}
        if case == 'vps_bind_create':
            return 'POST', '/api/spaces/alice/vps', binding
        first = saved(client.post('/api/spaces/alice/vps', json=binding))
        return 'PUT', '/api/spaces/alice/vps/'+first['id'], {
            **binding, 'name': 'Updated binding', 'expected_version': first['version'], 'idempotency_key': uuid.uuid4().hex}
    if case.startswith('profile_'):
        profile = {**body, 'label': 'Receipt Profile', 'policy': {'project_rules': [{'projects': ['project'], 'actions': ['read']}]}}
        if case == 'profile_create':
            return 'POST', '/api/spaces/alice/profiles', profile
        first = saved(client.post('/api/spaces/alice/profiles', json=profile))
        return 'PUT', '/api/spaces/alice/profiles/'+first['id'], {
            **profile, 'label': 'Updated Profile', 'expected_version': first['version'], 'idempotency_key': uuid.uuid4().hex}
    if case == 'space_create':
        return 'POST', '/api/spaces', {**body, 'label': 'Receipt Space'}
    if case == 'space_update':
        return 'PUT', '/api/spaces/alice', {**body, 'label': 'Updated Space', 'expected_version': 1}
    assert case == 'member'
    return 'PUT', '/api/spaces/alice/members/bob', {
        **body, 'policy': {'project_rules': [{'projects': ['project'], 'actions': ['read']}]}, 'expected_version': 0}


def durable_snapshot(store, *, audit=False):
    tables = ['spaces', 'memberships', 'storage_pools', 'storage_mappings', 'space_sources', 'space_source_mappings',
              'projects', 'project_shell_settings', 'project_execution_settings', 'space_execution_settings',
              'space_devices', 'vps_connections', 'space_vps', 'vps_projects', 'access_profiles',
              'workspace_requests', 'operations']
    if audit:
        tables.append('audit')
    # Read committed state from an independent connection, including receipts and
    # operation records, so a zero Runner-call count cannot hide another write.
    with sqlite3.connect((store.directory/'hub.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        return {table: db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in tables}


def forbid_dispatch(monkeypatch, app):
    async def forbidden(*args, **kwargs):
        pytest.fail('Configuration receipt recovery must not dispatch Runner work')
    monkeypatch.setattr(app.state.runtime, 'dispatch', forbidden)


@pytest.mark.parametrize('case', CASES)
def test_authorized_replay_returns_original_receipt_without_side_effects(web, monkeypatch, case):
    client, app = web
    forbid_dispatch(monkeypatch, app)
    method, path, body = mutation_request(client, case)
    first = client.request(method, path, json=body)
    result = saved(first)
    before = durable_snapshot(app.state.store, audit=True)
    replay = client.request(method, path, json=body)
    assert replay.status_code == first.status_code, replay.text
    assert replay.json() == result
    assert durable_snapshot(app.state.store, audit=True) == before
    # The original expected_version is deliberately retained, even though the
    # successful write advanced it. Changed arguments must still conflict.
    changed = {**body, 'enabled': False} if case == 'device' else {**body}
    if case != 'device':
        if 'name' in changed:
            changed['name'] += ' changed'
        elif 'label' in changed:
            changed['label'] += ' changed'
        else:
            changed['expected_version'] += 1
    conflict = client.request(method, path, json=changed)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert durable_snapshot(app.state.store) == {k: v for k, v in before.items() if k != 'audit'}


@pytest.mark.parametrize('case', CASES)
def test_every_mutation_replay_requires_current_resource_authority(web, monkeypatch, case):
    client, app = web
    store = app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    method, path, body = mutation_request(client, case)
    first = saved(client.request(method, path, json=body))
    if case == 'pool':
        store.execute("UPDATE storage_pools SET owner_user_id='bob' WHERE id='pool'")
    elif case in {'vps_create', 'vps_update'}:
        store.execute("UPDATE vps_connections SET owner_user_id='bob' WHERE id=?", (first['id'],))
    else:
        restrict_management(store, first['id'] if case == 'space_create' else 'alice')
    forbid_dispatch(monkeypatch, app)
    before = durable_snapshot(store)
    replay = client.request(method, path, json=body)
    assert replay.status_code == (404 if case in {'pool', 'vps_create', 'vps_update'} else 403), replay.text
    assert durable_snapshot(store) == before


@pytest.mark.parametrize('case', CASES)
def test_deleted_or_inactive_resource_cannot_disclose_old_receipt(web, monkeypatch, case):
    client, app = web
    store = app.state.store
    method, path, body = mutation_request(client, case)
    first = saved(client.request(method, path, json=body))
    if case == 'pool':
        # Remove dependent fixture resources in FK order, retaining the receipt.
        for sql in ('DELETE FROM projects', 'DELETE FROM space_source_mappings',
                    'DELETE FROM space_sources', 'DELETE FROM storage_mappings', 'DELETE FROM storage_pools'):
            store.execute(sql)
    elif case in {'shell', 'project_execution'}:
        store.execute("DELETE FROM projects WHERE id='project'")
    elif case == 'device':
        store.execute("DELETE FROM space_devices WHERE space_id='alice' AND device_id='a'")
    elif case in {'vps_create', 'vps_update'}:
        store.execute('DELETE FROM vps_connections WHERE id=?', (first['id'],))
    elif case.startswith('vps_bind_'):
        store.execute('DELETE FROM space_vps WHERE id=?', (first['id'],))
    elif case.startswith('profile_'):
        store.execute('DELETE FROM access_profiles WHERE id=?', (first['id'],))
    elif case == 'member':
        store.execute("DELETE FROM memberships WHERE space_id='alice' AND user_id='bob'")
    else:
        store.execute('UPDATE spaces SET active=0 WHERE id=?', (first['id'] if case == 'space_create' else 'alice',))
    forbid_dispatch(monkeypatch, app)
    before = durable_snapshot(store)
    replay = client.request(method, path, json=body)
    assert replay.status_code == 404, replay.text
    assert durable_snapshot(store) == before


@pytest.mark.parametrize('case', ['pool', 'shell'])
@pytest.mark.parametrize('revocation', ['account', 'epoch', 'membership', 'other_account'])
def test_replay_preserves_live_identity_and_request_isolation(web, case, revocation):
    client, app = web
    store = app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    method, path, body = mutation_request(client, case)
    saved(client.request(method, path, json=body))
    if revocation == 'account':
        store.execute("UPDATE iam_users SET active=0 WHERE user_id='alice'")
    elif revocation == 'epoch':
        store.execute("UPDATE iam_users SET epoch=epoch+1 WHERE user_id='alice'")
    elif revocation == 'membership':
        store.execute("UPDATE memberships SET active=0 WHERE space_id='alice' AND user_id='alice'")
    else:
        login = client.post('/api/login', json={'username': 'bob', 'password': 'a-valid-test-password'})
        client.headers['X-RD-CSRF'] = saved(login)['csrf']
    before = durable_snapshot(store)
    response = client.request(method, path, json=body)
    # Global inventory ownership is independent of any Space membership.
    expected = 200 if (case, revocation) == ('pool', 'membership') else (
        401 if revocation in {'account', 'epoch'} else 404 if case == 'pool' else 403)
    assert response.status_code == expected, response.text
    assert durable_snapshot(store) == before


def test_pool_receipt_remains_available_with_live_instance_admin_authority(web):
    client, app = web
    store = app.state.store
    body = pool_body()
    first = saved(client.put('/api/storage-pools/pool', json=body))
    store.execute("UPDATE storage_pools SET owner_user_id='bob' WHERE id='pool'")
    store.execute("UPDATE iam_users SET instance_admin=1 WHERE user_id='alice'")
    before = durable_snapshot(store)
    assert saved(client.put('/api/storage-pools/pool', json=body)) == first
    assert durable_snapshot(store) == before
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    assert client.put('/api/storage-pools/pool', json=body).status_code == 404


@pytest.mark.parametrize('case', ['device', 'vps_bind_create', 'vps_bind_update'])
def test_binding_replay_rechecks_inventory_ownership(web, case):
    client, app = web
    store = app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    method, path, body = mutation_request(client, case)
    saved(client.request(method, path, json=body))
    store.execute("UPDATE devices SET owner_user_id='bob' WHERE id='a'")
    before = durable_snapshot(store)
    assert client.request(method, path, json=body).status_code == 404
    assert durable_snapshot(store) == before


@pytest.mark.parametrize('case', ['vps_bind_create', 'vps_bind_update'])
@pytest.mark.parametrize('revocation', ['vps_owner', 'project_removed'])
def test_vps_binding_receipt_requires_original_resource_relationships(web, case, revocation):
    client, app = web
    store = app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    method, path, body = mutation_request(client, case)
    saved(client.request(method, path, json=body))
    if revocation == 'vps_owner':
        store.execute("UPDATE vps_connections SET owner_user_id='bob' WHERE id=?", (body['resource_id'],))
    else:
        store.execute("DELETE FROM projects WHERE id='project'")
    before = durable_snapshot(store)
    assert client.request(method, path, json=body).status_code == 404
    assert durable_snapshot(store) == before


def test_member_receipt_requires_authority_over_current_administrator(web):
    client, app = web
    store = app.state.store
    method, path, body = mutation_request(client, 'member')
    saved(client.request(method, path, json=body))
    store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='bob'",
                  (json.dumps({'manage': ['workspace']}),))
    store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='alice'",
                  (json.dumps({'manage': ['members']}),))
    before = durable_snapshot(store)
    assert client.request(method, path, json=body).status_code == 403
    assert durable_snapshot(store) == before


def test_independently_authorized_account_cannot_recover_another_accounts_receipt(web):
    client, app = web
    store = app.state.store
    body = pool_body()
    saved(client.put('/api/storage-pools/pool', json=body))
    store.execute("UPDATE iam_users SET instance_admin=1 WHERE user_id='bob'")
    login = client.post('/api/login', json={'username': 'bob', 'password': 'a-valid-test-password'})
    client.headers['X-RD-CSRF'] = saved(login)['csrf']
    assert client.get('/api/storage-pools/pool/mappings').status_code == 200
    before = durable_snapshot(store)
    response = client.put('/api/storage-pools/pool', json=body)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'VERSION_CHANGED'
    assert durable_snapshot(store) == before


def test_new_space_binding_derives_runner_authority_from_resource_after_transfer(web):
    client,app=web
    store=app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    method,path,body=mutation_request(client,'vps_bind_create')
    store.execute("UPDATE devices SET owner_user_id='bob' WHERE id='a'")
    before=durable_snapshot(store)
    result=client.request(method,path,json=body)
    assert result.status_code==404 and result.json()['error']['code']=='INVENTORY_RESOURCE_NOT_FOUND'
    assert durable_snapshot(store)==before
