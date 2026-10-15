"""Logical source views and independent file/Git/test device acceptance."""
from __future__ import annotations

import json
import sys
import uuid

from hub.store import Store
from shared.util import atomic_json
from tests.routing_support import routing_stack


def test_shared_project_files_git_rch_and_safe_unbind(routing_stack):
    r = routing_stack
    listed = r.get('/api/projects')['projects']
    p = next(p for p in listed if p['id'] == r.project['id'])
    assert p['routing_managed'] and p['device_id'] == r.builtin
    assert {v['action']: v.get('device_id') for v in p['execution_routes']}['git'] == r.s.device
    store = Store(r.s.hubdir)
    try:
        logical = store.one('SELECT * FROM projects WHERE id=?', (p['id'],))
        assert logical['device_id'] is None and logical['root'] is None
    finally:
        store.close()
    written = r.call('write', {'path': 'new.txt', 'content': 'uncommitted-nfs-bytes', 'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex})
    assert written.get('sha256') or written.get('state') == 'succeeded', written
    git = r.exec(['git', 'status', '--porcelain'], action='git')
    assert git['device_id'] == r.s.device and git['state'] == 'succeeded', git
    assert 'new.txt' in git['result']['data']['output']
    key = uuid.uuid4().hex
    test = r.exec([sys.executable, '-c', 'from pathlib import Path; assert Path("new.txt").read_text() == "uncommitted-nfs-bytes"; print("RCH fixture verified")'], key=key)
    assert test['state'] == 'succeeded', test
    assert test['device_id'] == r.s.device
    assert test['result']['data']['provider'] == 'rch'
    assert test['result']['data']['rch']['terminal_acknowledged'] is True
    again = r.exec([sys.executable, '-c', 'from pathlib import Path; assert Path("new.txt").read_text() == "uncommitted-nfs-bytes"; print("RCH fixture verified")'], key=key)
    assert again['id'] == test['id']
    assert r.fake.with_suffix('.count').read_text().splitlines() == ['exec']
    assert r.post('/api/project-routing/projects', r.batch)['replayed'] is True
    r.put('/api/projects/' + p['id'] + '/source', {'expected_version': 1, 'source_root_id': None})
    after = next(p for p in r.get('/api/projects')['projects'] if p['id'] == r.project['id'])
    assert not after['source_bound'] and not after['device_id']
    assert (r.s.imago / 'new.txt').read_text() == 'uncommitted-nfs-bytes'
    assert r.get('/api/project-routing')['sources'][0]['id'] == r.source['id']
    response = r.s.call('read', {'project': p['id'], 'path': 'new.txt'}, raw=True)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PROJECT_SOURCE_UNBOUND'


def test_space_recipes_hooks_and_unknown_rch_recovery(routing_stack):
    r = routing_stack
    r.put('/api/project-routing/execution', {'expected_version': 0, 'settings': {
        'hooks': [{'name': 'remote-tests', 'actions': ['test'], 'handler': 'require_rch'}],
        'tests': [{'name': 'unit', 'argv': [sys.executable, '-c', 'print("configured space test")']}]
    }})
    result = r.exec(task='unit')
    assert result['state'] == 'succeeded', result
    assert 'configured space test' in result['result']['data']['output']
    # A dead wrapper does not prove the remote task stopped. Hold the source.
    timeout = r.exec([sys.executable, '-c', 'import time; time.sleep(20)'], timeout_seconds=1)
    assert timeout['state'] == 'needs_review', timeout
    control = r.call('routed_job_control', {'original_operation_id': timeout['id'], 'action': 'recover',
        'timeout_seconds': 3, 'idempotency_key': uuid.uuid4().hex})
    assert control['state'] == 'succeeded', control
    assert control['result']['data']['command_replayed'] is False
    assert control['result']['data']['rch']['terminal_acknowledged']
    assert r.fake.with_suffix('.count').read_text().splitlines() == ['exec', 'exec']
    assert r.get('/api/operations/' + timeout['id'])['source_lock_released'] == 1
    r.put('/api/projects/' + r.project['id'] + '/execution', {'expected_version': 0,
        'settings': {'hooks': [{'name': 'stop', 'actions': ['test'], 'handler': 'deny', 'reason': 'fixture policy'}]}})
    denied = r.s.call('exec', {'project': r.project['id'], 'action': 'test', 'argv': ['echo', 'no'],
        'idempotency_key': uuid.uuid4().hex}, raw=True)
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'EXECUTION_HOOK_DENIED'


def test_subfolder_traversal_symlink_and_disabled_route(routing_stack):
    r = routing_stack
    r.s.nexus.joinpath('secret.txt').write_text('sibling')
    bad = {**r.batch, 'idempotency_key': uuid.uuid4().hex, 'projects': [{'alias': 'escape', 'subpath': '../outside'}]}
    assert r.client.post('/api/project-routing/projects', json=bad).status_code == 422
    r.s.root.joinpath('link').symlink_to(r.s.nexus, target_is_directory=True)
    bad['projects'] = [{'alias': 'link', 'subpath': 'link'}]
    assert r.client.post('/api/project-routing/projects', json=bad).status_code == 409
    assert not any(p['alias'] in {'escape', 'link'} for p in r.get('/api/projects')['projects'])
    version = r.put('/api/projects/' + r.project['id'] + '/routing/git', {'expected_version': 1, 'mode': 'disabled'})
    assert version['routing_version'] == 2
    denied = r.s.call('exec', {'project': r.project['id'], 'action': 'git', 'argv': ['git', 'status'],
        'idempotency_key': uuid.uuid4().hex}, raw=True)
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'PROJECT_ROUTE_DISABLED'


def test_fixed_grant_route_snapshot_does_not_follow_new_targets(routing_stack):
    r = routing_stack
    role = r.post('/api/access-roles', {'label': 'Routed fixture', 'idempotency_key': uuid.uuid4().hex,
        'project_rules': [{'projects': [r.project['id']], 'actions': ['read', 'write', 'execute']}],
        'route_rules': [{'route_id': route['id'], 'projects': [r.project['id']]} for route in r.routes.values()]})
    connection = r.post('/api/client-connections', {'name': 'fixed routing', 'role_id': role['id'],
        'expected_role_version': role['version'], 'authorization_mode': 'fixed', 'idempotency_key': uuid.uuid4().hex})
    result = r.s.mcp('project_query', {'project': r.project['id'], 'operation': 'routes'}, token_value=connection['token'])
    assert not result.get('isError'), result
    assert any(route['authorized'] for route in result['structuredContent']['routes'])
    r.put('/api/action-routes/test', {'expected_version': 1, 'family': 'test', 'device_id': r.s.device,
        'settings': {'provider': 'rch', 'rch': {'worker': 'different-worker'}}})
    denied = r.s.mcp('exec', {'project': r.project['id'], 'action': 'test', 'argv': ['echo', 'denied'],
        'idempotency_key': uuid.uuid4().hex}, token_value=connection['token'])
    assert denied.get('isError'), denied
    assert 'ROUTE_FIXED_CONSENT_CHANGED' in json.dumps(denied)


def test_missing_local_rch_has_no_fallback(routing_stack):
    r = routing_stack
    r.s.stop_agent()
    r.s.config['rch']['enabled'] = False
    atomic_json(r.s.config_path, r.s.config)
    r.s.start_agent()
    result = r.exec([sys.executable, '-c', 'from pathlib import Path; Path("should-not-exist").write_text("bad")'])
    assert result['state'] == 'failed', result
    assert result['result']['error']['code'] == 'RCH_LOCAL_DISABLED'
    assert not r.s.imago.joinpath('should-not-exist').exists()
