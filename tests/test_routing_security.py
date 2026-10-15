"""Revocation, preflight and shared-source recovery using real Hub/Agent calls."""
from __future__ import annotations

import json
import sys
import uuid

from tests.routing_support import routing_stack


def test_receipt_get_list_and_wait_enforce_live_route_permission(routing_stack):
    r = routing_stack
    role = r.post('/api/access-roles', {'label': 'Private test results', 'idempotency_key': uuid.uuid4().hex,
        'project_rules': [{'projects': [r.project['id']], 'actions': ['read', 'execute']}],
        'route_rules': [{'route_id': r.routes['test']['id'], 'projects': [r.project['id']]}]})
    grant = r.post('/api/client-connections', {'name': 'Revocable routing', 'role_id': role['id'],
        'expected_role_version': role['version'], 'authorization_mode': 'role', 'confirm_dynamic_role': True,
        'idempotency_key': uuid.uuid4().hex})
    call = r.s.mcp('exec', {'project': r.project['id'], 'action': 'test',
        'argv': [sys.executable, '-c', 'print("private-route-data")'], 'idempotency_key': uuid.uuid4().hex,
        'yield_seconds': 0}, token_value=grant['token'])
    assert not call.get('isError'), call
    identifier = call['structuredContent']['operation_id']
    completed = r.s.poll(identifier, timeout=45)
    assert completed['state'] == 'succeeded', completed
    r.put('/api/access-roles/' + role['id'], {'label': role['label'], 'expected_version': role['version'],
        'project_rules': role['project_rules'], 'device_rules': role['device_rules'], 'route_rules': []})
    for operation in ('get', 'wait'):
        response = r.s.mcp('task_query', {'operation': operation, 'operation_ids': [identifier]}, token_value=grant['token'])
        assert 'ROUTE_RECEIPT_DENIED' in json.dumps(response), response
        assert 'private-route-data' not in json.dumps(response)
    response = r.s.mcp('task_query', {'operation': 'list', 'project': r.project['id']}, token_value=grant['token'])
    assert identifier not in json.dumps(response), response


def test_rch_state_home_mismatch_blocks_test_before_launch(routing_stack):
    r = routing_stack
    r.fake.write_text(r.fake.read_text().replace("state = pathlib.Path(os.environ['RCH_STATE_HOME'])",
        "state = pathlib.Path(__file__).parent / 'incompatible-rch-state'"))
    outcome = r.exec([sys.executable, '-c', 'raise AssertionError("must not run")'])
    assert outcome['state'] == 'failed', outcome
    assert outcome['result']['error']['code'] == 'RCH_INCOMPATIBLE'
    assert not r.fake.with_suffix('.count').exists()


def test_unknown_remote_outcome_holds_source_not_independent_sibling(routing_stack):
    r = routing_stack
    uncertain = r.exec([sys.executable, '-c', 'import time; time.sleep(20)'], timeout_seconds=1)
    assert uncertain['state'] == 'needs_review', uncertain
    blocked = r.call('write', {'path': 'after-recovery.txt', 'content': 'after', 'expected_sha256': 'new',
        'idempotency_key': uuid.uuid4().hex}, wait=False)
    assert blocked.get('pending'), blocked
    assert not r.s.imago.joinpath('after-recovery.txt').exists()
    independent = r.s.call('write', {'project': r.projects[1]['id'], 'path': 'independent.txt',
        'content': 'independent', 'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex})
    if independent.get('pending'):
        assert r.s.poll(independent['operation_id'], timeout=20)['state'] == 'succeeded'
    assert r.s.nexus.joinpath('independent.txt').read_text() == 'independent'
    recovery = r.call('routed_job_control', {'original_operation_id': uncertain['id'], 'action': 'recover',
        'timeout_seconds': 3, 'idempotency_key': uuid.uuid4().hex})
    assert recovery['state'] == 'succeeded', recovery
    assert r.s.poll(blocked['operation_id'], timeout=20)['state'] == 'succeeded'
    assert r.s.imago.joinpath('after-recovery.txt').read_text() == 'after'
    assert r.fake.with_suffix('.count').read_text().splitlines() == ['exec']
