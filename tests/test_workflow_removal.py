"""Retired workflows have no storage/runtime/API path in the new workspace model."""
import asyncio
from contextlib import closing
import json
import sqlite3

import pytest

from hub.core_tools import help_result
from hub.runtime import Runtime
from hub.store import Store
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, tool_definitions
from shared.util import DevError
from tests.operation_fixture import env, call, operation  # noqa: F401
from tests.identity_fixtures import team  # noqa: F401

NAMES=['workflows_create','workflows_list','workflows_get','workflows_update','workflows_handoff']
ACTIONS=['workflow_create','workflow_list','workflow_get','workflow_update','handoff']


def test_fresh_store_and_catalog_have_no_workflow_subsystem(env):
    assert not env[0].store.all("SELECT name FROM sqlite_master WHERE name LIKE 'workflow%'")
    assert not hasattr(env[0],'workflows')
    assert not set(NAMES)&(TOOLS.keys()|OUTPUT_SCHEMAS.keys())
    assert 'workflow' not in json.dumps(tool_definitions())
    assert not set(ACTIONS)&set(help_result('workspace')['tools']['workspace']['operations'])
    for name in ACTIONS:
        with pytest.raises(DevError) as denied:help_result('workspace',name)
        assert denied.value.code=='UNKNOWN_OPERATION'


@pytest.mark.parametrize('name',NAMES)
def test_private_runtime_rejects_workflows_and_keeps_real_operation_control(env,name):
    pending=operation(env,finish=False)
    before=env[0].store.all('SELECT * FROM operations')
    with pytest.raises(DevError) as denied:
        asyncio.run(env[0].invoke(name,{'workflow_id':'retired-not-created'},env[1]))
    assert denied.value.code=='UNKNOWN_TOOL'
    assert env[0].store.all('SELECT * FROM operations')==before
    assert call(env,'operations_cancel',operation_id=pending)['cancel_requested']
    assert call(env,'operations_get',operation_id=pending)['state']=='cancelled'
    assert not env[0].store.all("SELECT name FROM sqlite_master WHERE name LIKE 'workflow%'")


@pytest.mark.parametrize('facade',['workspace','project_query'])
@pytest.mark.parametrize('action',ACTIONS)
def test_facades_reject_retired_operations(env,facade,action):
    with pytest.raises(DevError) as denied:
        asyncio.run(env[0].invoke(facade,{'operation':action,'project':'P',
            'options':{'workflow_id':'a'*32}},env[1]))
    assert denied.value.code=='INVALID_ARGUMENTS'
    assert not env[0].store.all('SELECT id FROM operations')


@pytest.mark.parametrize('selector,value',[('workflow_id','a'*32),('workflow_cursor',''),('evidence_offset',0)])
def test_dashboard_rejects_retired_selectors(env,selector,value):
    for name,args in [('workspace_status',{'project':'P',selector:value}),
        ('workspace',{'operation':'dashboard','project':'P','options':{selector:value}}),
        ('project_query',{'operation':'dashboard','project':'P','options':{selector:value}})]:
        with pytest.raises(DevError) as denied:asyncio.run(env[0].invoke(name,args,env[1]))
        assert denied.value.code=='INVALID_ARGUMENTS'


def test_current_receipts_survive_restart_without_reintroducing_workflow_tables(env):
    identifier=operation(env)
    before=env[0].store.all('SELECT * FROM operations')
    with closing(Store(env[0].store.directory)) as second:
        assert second.all('SELECT * FROM operations')==before
        assert not second.all("SELECT name FROM sqlite_master WHERE name LIKE 'workflow%'")
        assert call((Runtime(second),env[1]),'operations_get',operation_id=identifier)['state']=='succeeded'
        assert not second.all('PRAGMA foreign_key_check')


@pytest.mark.parametrize('table',['workflows','workflow_events','workflow_replays'])
def test_old_workflow_storage_is_a_hard_startup_failure_not_retained_inert_data(env,table):
    store=env[0].store
    store.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,detail TEXT)')
    store.execute(f'INSERT INTO {table} VALUES(?,?)',('fixture-only','must-not-be-converted'))
    with closing(sqlite3.connect(store.directory/'hub.sqlite3')) as db:before=list(db.iterdump())
    with pytest.raises(RuntimeError,match='WORKSPACE_DATABASE_REQUIRED'):Store(store.directory)
    with closing(sqlite3.connect(store.directory/'hub.sqlite3')) as db:assert list(db.iterdump())==before


def test_workflow_sharing_and_http_dispatch_have_no_authority_path(team):
    app,b=team
    before=app.state.store.all('SELECT * FROM operations')
    for browser in (b['owner'],b['alice']):
        response=browser.put('/api/iam/share/workflow/'+'a'*32,json={'visibility':'space'})
        assert response.status_code==404 and 'location' not in response.headers
        for name in NAMES:
            response=browser.post('/api/tools/call',json={'tool':name,'arguments':{'workflow_id':'a'*32}})
            assert response.status_code==404 and response.json()['error']['code']=='UNKNOWN_TOOL'
    assert app.state.store.all('SELECT * FROM operations')==before
    assert not app.state.store.all("SELECT name FROM sqlite_master WHERE name LIKE 'workflow%'")
