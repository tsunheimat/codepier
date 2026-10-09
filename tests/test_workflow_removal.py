"""Removed workflow contracts fail closed; current operations remain independent."""
import asyncio
import json

import pytest

from hub.core_tools import help_result
from hub.runtime import Runtime
from hub.store import Store
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, tool_definitions
from shared.util import DevError
from tests.historical_workflows import seed_workflow
from tests.operation_fixture import env, call, operation
from tests.test_iam_integration import team as team

NAMES = ['workflows_create', 'workflows_list', 'workflows_get', 'workflows_update', 'workflows_handoff']
ACTIONS = ['workflow_create', 'workflow_list', 'workflow_get', 'workflow_update', 'handoff']


def snapshot(store):
    return {table: store.all('SELECT * FROM ' + table) for table in
            ('workflows', 'workflow_events', 'workflow_replays', 'operations')}


def test_fresh_store_and_catalog_have_no_workflow_subsystem(env):
    assert not env[0].store.all("SELECT name FROM sqlite_master WHERE name LIKE 'workflow%'")
    assert not hasattr(env[0], 'workflows')
    assert not set(NAMES) & (TOOLS.keys() | OUTPUT_SCHEMAS.keys())
    assert 'workflow' not in json.dumps(tool_definitions())
    assert not set(ACTIONS) & set(help_result('workspace')['tools']['workspace']['operations'])
    for name in ACTIONS:
        with pytest.raises(DevError) as denied: help_result('workspace', name)
        assert denied.value.code == 'UNKNOWN_OPERATION'


@pytest.mark.parametrize('name', NAMES)
def test_private_runtime_has_no_workflow_dispatch_and_cancellation_still_works(env, name):
    seed_workflow(env[0].store, project_id='p', user_id='u', grant_id='g', key='old-key')
    pending = operation(env, finish=False)
    before = snapshot(env[0].store)
    with pytest.raises(DevError) as denied:
        asyncio.run(env[0].invoke(name, {'workflow_id': before['workflows'][0]['id']}, env[1]))
    assert denied.value.code == 'UNKNOWN_TOOL'
    assert snapshot(env[0].store) == before
    assert call(env, 'operations_cancel', operation_id=pending)['cancel_requested']
    assert call(env, 'operations_get', operation_id=pending)['state'] == 'cancelled'


@pytest.mark.parametrize('facade', ['workspace', 'project_query'])
@pytest.mark.parametrize('action', ACTIONS)
def test_facades_reject_retired_operations(env, facade, action):
    with pytest.raises(DevError) as denied:
        asyncio.run(env[0].invoke(facade, {'operation': action, 'project': 'P',
            'options': {'workflow_id': 'a' * 32}}, env[1]))
    assert denied.value.code == 'INVALID_ARGUMENTS'
    assert not env[0].store.all('SELECT id FROM operations')


@pytest.mark.parametrize('selector,value', [('workflow_id', 'a' * 32), ('workflow_cursor', ''), ('evidence_offset', 0)])
def test_dashboard_rejects_retired_selectors(env, selector, value):
    for name, args in [('workspace_status', {'project': 'P', selector: value}),
                       ('workspace', {'operation': 'dashboard', 'project': 'P', 'options': {selector: value}}),
                       ('project_query', {'operation': 'dashboard', 'project': 'P', 'options': {selector: value}})]:
        with pytest.raises(DevError) as denied: asyncio.run(env[0].invoke(name, args, env[1]))
        assert denied.value.code == 'INVALID_ARGUMENTS'


def test_upgrade_leaves_inert_rows_and_receipts_unchanged(env):
    store = env[0].store
    receipt = seed_workflow(store, project_id='p', user_id='u', grant_id='g', key='old-key',
                           steps=[{'id': 's1', 'state': 'completed', 'summary': 'Historical content'}])
    identifier = operation(env)
    before = snapshot(store)
    schema = store.all("SELECT name,sql FROM sqlite_master WHERE name LIKE 'workflow%' ORDER BY name")
    second = Store(store.directory)
    try:
        assert snapshot(second) == before
        assert second.all("SELECT name,sql FROM sqlite_master WHERE name LIKE 'workflow%' ORDER BY name") == schema
        runtime = Runtime(second)
        assert not hasattr(runtime, 'workflows')
        assert call((runtime, env[1]), 'operations_get', operation_id=identifier)['state'] == 'succeeded'
        assert second.all('PRAGMA foreign_key_check') == []
        assert second.one('SELECT id FROM workflows')['id'] == receipt['workflow_id']
    finally: second.close()


def test_workflow_sharing_and_http_dispatch_are_gone_for_every_user(team):
    app, browsers = team
    row = seed_workflow(app.state.store, project_id='project-team', user_id='owner', key='old-share')
    before = snapshot(app.state.store)
    for browser in (browsers['owner'], browsers['alice']):
        response = browser.put('/api/iam/share/workflow/' + row['workflow_id'], json={'visibility': 'space'})
        assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_RESOURCE'
        for name in NAMES:
            response = browser.post('/api/tools/call', json={'tool': name, 'arguments': {'workflow_id': row['workflow_id']}})
            assert response.status_code == 404 and response.json()['error']['code'] == 'UNKNOWN_TOOL'
    assert snapshot(app.state.store) == before
