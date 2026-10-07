"""Regression tests for persisted progress, isolation and honest execution evidence."""
import asyncio
import dataclasses
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator

from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.contracts import TOOLS, OUTPUT_SCHEMAS
from shared.util import DevError


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "hub")
    from tests.legacy_iam_fixture import seed_owner
    seed_owner(store,'u','fixture')
    seed_owner(store,'viewer','other')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('d','fixture','fixture',?)", (time.time(),))
    for id in ("p", "p2"):
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,allow_tasks,created) VALUES (?,?,?,'d',?,1,?)",
                      (id, id.upper(), id, str(tmp_path / id), time.time()))
    from tests.legacy_iam_fixture import seed_grant
    seed_grant(store,'g')
    seed_grant(store,'other')
    seed_grant(store,'g2')
    runtime = Runtime(store)
    principal = Principal("mcp:g:fixture", "u", {"read", "write", "execute"}, ["p", "p2"], "g")
    yield runtime, principal
    store.close()


def call(env, name, **args):
    # Persist the fixture's intended credential policy: runtime now deliberately
    # re-reads grants instead of trusting a modified in-memory Principal.
    store,principal=env[0].store,env[1]
    original=store.one('SELECT scopes,projects FROM grants WHERE id=?',(principal.grant_id,)) if principal.grant_id else None
    if original:store.execute('UPDATE grants SET scopes=?,projects=? WHERE id=?',(json.dumps(sorted(principal.scopes)),json.dumps(principal.projects),principal.grant_id))
    try:
        result = asyncio.run(env[0].invoke(name, args, principal))
        Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(result)
        return result
    finally:
        if original:store.execute('UPDATE grants SET scopes=?,projects=? WHERE id=?',(original['scopes'],original['projects'],principal.grant_id))


def create(env, **args):
    """Historical fixture setup; the product no longer creates workflows."""
    from tests.historical_workflows import seed_workflow
    project=env[0].project(args.get('project','P'),env[1])
    steps=[{'id':'s1','title':'Review','acceptance':'Read actual files','state':'pending','summary':'','evidence':[]},
           {'id':'s2','title':'Verify','acceptance':'Check result','state':'pending','summary':'','evidence':[]}]
    return seed_workflow(env[0].store,project_id=project['id'],user_id=env[1].user_id,
        grant_id=env[1].grant_id,actor=env[1].actor,space_id=env[1].space_id,
        title=args.get('title','Historical review'),steps=steps,state='active')


def update(env, receipt, **args):
    """Populate old event/evidence history for read-projection tests only."""
    store=env[0].store; row=store.one('SELECT * FROM workflows WHERE id=?',(receipt['workflow_id'],))
    version=row['version']+1;now=time.time();steps=json.loads(row['steps'])
    evidence=[{'operation_id':identifier} for identifier in args.get('evidence',[])]
    for step in steps:
        if step['id']==args.get('step_id'):
            step.update(state=args.get('step_state',step['state']),summary=args.get('summary','Historical result'),evidence=evidence)
    with store.transaction():
        store.db.execute('UPDATE workflows SET steps=?,summary=?,version=?,updated=? WHERE id=?',
            (json.dumps(steps),args.get('summary','Historical result'),version,now,row['id']))
        store.db.execute('INSERT INTO workflow_events(workflow_id,action,summary,evidence,at,version) VALUES(?,?,?,?,?,?)',
            (row['id'],'checkpoint','Historical result',json.dumps(evidence),now,version))
    return {**receipt,'version':version}


def get(env, receipt, **args):
    return call(env, "workflows_get", workflow_id=receipt["workflow_id"], **args)


def operation(env, *, project="p", grant="g", created=None, exit_code=0, output="passed", finish=True, **data):
    runtime, principal = env
    identifier = uuid.uuid4().hex
    now = time.time() if created is None else created
    runtime.store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated) VALUES (?,'d',?,?,?,'tasks_run','{}','fixture','running',?,?)",
                          (identifier, project, principal.actor, grant, now, now))
    if finish:
        runtime.complete({"id": identifier}, {"ok": True, "data": {"exit_code": exit_code, "output": output, **data}})
    return identifier


def test_archives_preserve_steps_events_and_replay_bytes_across_reopen(env):
    receipt=create(env)
    identifier=operation(env)
    receipt=update(env,receipt,step_id='s1',step_state='completed',evidence=[identifier])
    before=env[0].store.one('SELECT * FROM workflows WHERE id=?',(receipt['workflow_id'],))
    second=Store(env[0].store.directory)
    try:
        restored=call((Runtime(second),env[1]),'workflows_get',workflow_id=receipt['workflow_id'])
        assert restored['retired'] and restored['can_update'] is False
        assert restored['next_step'] is None and restored['next_action'] is None
        assert restored['steps'][0]['evidence'][0]['operation_id']==identifier
        assert second.one('SELECT * FROM workflows WHERE id=?',(receipt['workflow_id'],))==before
    finally:second.close()


@pytest.mark.parametrize('name',['workflows_create','workflows_update'])
def test_retired_writes_are_explicit_and_leave_archive_and_operations_unchanged(env,name):
    receipt=create(env);op=operation(env,finish=False)
    before={table:env[0].store.all('SELECT * FROM '+table) for table in ('workflows','workflow_events','workflow_replays','operations')}
    args={'project':'P','title':'No new task','goal':'No tracking','idempotency_key':uuid.uuid4().hex} if name=='workflows_create' else {
        'workflow_id':receipt['workflow_id'],'expected_version':receipt['version'],'action':'checkpoint','summary':'Must not update','idempotency_key':uuid.uuid4().hex}
    with pytest.raises(DevError) as error:call(env,name,**args)
    assert error.value.code=='WORKFLOW_RETIRED'
    assert {table:env[0].store.all('SELECT * FROM '+table) for table in before}==before
    cancelled=call(env,'operations_cancel',operation_id=op)
    assert cancelled['cancel_requested']
    assert env[0].store.one('SELECT state FROM operations WHERE id=?',(op,))['state']=='cancelled'


def test_archive_list_and_get_still_enforce_exact_grant_and_mapping(env):
    receipt=create(env)
    other=dataclasses.replace(env[1],actor='mcp:other:fixture',grant_id='other')
    assert call((env[0],other),'workflows_list')['workflows']==[]
    with pytest.raises(DevError) as error:get((env[0],other),receipt)
    assert error.value.code=='WORKFLOW_NOT_FOUND'
    env[0].store.execute("UPDATE projects SET root='/different' WHERE id='p'")
    restored=get(env,receipt)
    assert restored['mapping_changed'] and restored['can_update'] is False


def test_archive_pagination_preserves_all_records_without_new_templates(env):
    ids={create(env)['workflow_id'] for _ in range(5)}
    seen=set();cursor=''
    while True:
        result=call(env,'workflows_list',limit=2,cursor=cursor)
        assert result['retired'] and result['templates']==[]
        seen.update(row['workflow_id'] for row in result['workflows'])
        cursor=result['next_cursor']
        if not cursor:break
    assert ids==seen
