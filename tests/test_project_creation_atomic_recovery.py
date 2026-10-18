"""Folder-bound creation commits atomically after original validation.

There is no device/root mapping API, Role-created-project selector or compatibility
upgrade. Faults below are injected only into disposable fixtures after real
membership/Profile authorization; original validation keys are not regenerated.
"""
from __future__ import annotations

import json
import uuid

import pytest

from hub import iam
from shared.util import DevError
from tests.current_model import CurrentModel
from tests.identity_fixtures import (team, must, call, data, set_member, make_profile,
    make_connection)  # noqa: F401


def body(**changes):
    return {'alias':'NewProject','folder_id':'folder-team','subpath':'sub',
        'idempotency_key':uuid.uuid4().hex,**changes}


def caller(b, kind, *, project_access=True):
    policy={'folder_rules':[{'folders':['folder-team'],'actions':['read','projects.create']}]}
    if project_access:policy['project_rules']=[{'all_projects':True,'actions':['read','write']}]
    if kind=='panel':
        set_member(b['owner'],'alice',{**policy,'manage':['resources']})
        return lambda values:b['alice'].post('/api/spaces/team/projects',json=values)
    set_member(b['owner'],'alice',policy)
    profile=make_profile(b['owner'],policy)
    connection=make_connection(b['alice'],profile,live=True)
    return lambda values:call(b['alice'],connection['token'],'projects_create',values)


def response_value(response, kind):
    return data(response) if kind=='mcp' else must(response,201)


def response_error(response, kind):
    if kind=='mcp':
        result=response.json()['result'];assert result['isError'],result
        return result['structuredContent']['error']
    assert response.status_code>=400,response.text
    return response.json()['error']


def validate_ok(calls, effect=None):
    async def validate(name,args,project,principal,**kwargs):
        assert name=='system_validate'
        assert '_validation_context' in project and '_source_boundary' in project
        calls.append((args['idempotency_key'],project['root'],principal.grant_id))
        if effect:effect()
        return {'root':project['root'],'writable':True,'allow_tasks':True}
    return validate


def intents(store):
    return store.all("SELECT key,value FROM meta WHERE key LIKE 'workspace-validation:%' ORDER BY key")


@pytest.mark.parametrize('kind',['panel','mcp'])
@pytest.mark.parametrize('failure',['commit','receipt','audit'])
def test_transaction_failure_rolls_back_and_reuses_original_validation(team, monkeypatch, kind, failure):
    app,b=team;store=app.state.store;runtime=app.state.runtime
    submit=caller(b,kind);values=body();validations=[];events=[]
    monkeypatch.setattr(runtime,'dispatch',validate_ok(validations))
    monkeypatch.setattr(runtime,'publish',lambda *args:events.append(args))
    resources=runtime.workspace_resources
    original=resources._commit_validated
    def fail_commit(*args,**kwargs):
        row=original(*args,**kwargs)
        assert store.one('SELECT id FROM projects WHERE id=?',(row['id'],))
        if failure=='receipt':return {**row,'non_json':object()}
        raise DevError('COMMIT_FIXTURE_FAILURE','rollback this isolated transaction',409)
    original_audit=iam.audit
    def fail_audit(store_,principal,action,*args,**kwargs):
        if action=='project.save:new':
            raise DevError('COMMIT_FIXTURE_FAILURE','rollback this isolated audit/receipt',409)
        return original_audit(store_,principal,action,*args,**kwargs)
    if failure=='audit':monkeypatch.setattr(iam,'audit',fail_audit)
    else:monkeypatch.setattr(resources,'_commit_validated',fail_commit)
    if failure=='receipt':
        with pytest.raises(TypeError):submit(values)
    else:assert response_error(submit(values),kind)['code']=='COMMIT_FIXTURE_FAILURE'
    assert not store.one('SELECT id FROM projects WHERE alias=?',(values['alias'],))
    assert not store.one("SELECT receipt FROM workspace_requests WHERE operation='project.save:new' AND request_key=?",(values['idempotency_key'],))
    assert not store.one("SELECT id FROM audit WHERE action='project.save:new'")
    assert not [event for event in events if event[0]=='project']
    before=intents(store);assert before
    monkeypatch.setattr(resources,'_commit_validated',original)
    monkeypatch.setattr(iam,'audit',original_audit)
    created=response_value(submit(values),kind)
    assert response_value(submit(values),kind)['id']==created['id']
    assert len({key for key,_,_ in validations})==1 and len(validations)==2
    assert intents(store)==before
    assert store.one('SELECT count(*) AS n FROM projects WHERE alias=?',(values['alias'],))['n']==1
    assert store.one("SELECT count(*) AS n FROM audit WHERE action='project.save:new'")['n']==1
    assert len([event for event in events if event[0]=='project'])==1


@pytest.mark.parametrize('kind',['panel','mcp'])
@pytest.mark.parametrize('change',['folder','membership','collision'])
def test_validation_wait_rechecks_live_source_and_authority_before_commit(team, monkeypatch, kind, change):
    app,b=team;store=app.state.store;submit=caller(b,kind);values=body()
    def changed():
        if change=='folder':store.execute("UPDATE space_folders SET device_path=device_path||'/changed',version=version+1 WHERE id='folder-team'")
        elif change=='membership':store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
        else:CurrentModel(store).project('competing',space='team',folder='folder-team',owner='owner',alias=values['alias'],subpath='elsewhere')
    monkeypatch.setattr(app.state.runtime,'dispatch',validate_ok([],changed))
    error=response_error(submit(values),kind)
    assert error['code']=={'folder':'VALIDATION_CHANGED','membership':'SPACE_FORBIDDEN','collision':'ALIAS_EXISTS'}[change]
    assert not store.one("SELECT id FROM projects WHERE alias=? AND owner_user_id='alice'",(values['alias'],))
    assert not store.one("SELECT receipt FROM workspace_requests WHERE operation='project.save:new'")
    assert not store.one("SELECT id FROM audit WHERE action='project.save:new'")


@pytest.mark.parametrize('kind',['panel','mcp'])
@pytest.mark.parametrize('invalid',['canonical','readonly','tasks'])
def test_device_validation_must_confirm_exact_source_capabilities(team, monkeypatch, kind, invalid):
    app,b=team;submit=caller(b,kind)
    async def invalid_result(name,args,project,principal,**kwargs):
        return {'root':project['root']+'/elsewhere' if invalid=='canonical' else project['root'],
            'writable':invalid!='readonly','allow_tasks':invalid!='tasks'}
    monkeypatch.setattr(app.state.runtime,'dispatch',invalid_result)
    error=response_error(submit(body()),kind)
    assert error['code']==('VALIDATION_PATH_CHANGED' if invalid=='canonical' else 'LOCAL_CAPABILITY_DENIED')
    assert not app.state.store.one("SELECT id FROM projects WHERE alias='NewProject'")


@pytest.mark.parametrize('kind',['panel','mcp'])
def test_pending_validation_keeps_key_and_never_double_commits(team, monkeypatch, kind):
    app,b=team;submit=caller(b,kind);values=body();calls=[]
    async def pending(name,args,project,principal,**kwargs):
        calls.append(args['idempotency_key'])
        return {'pending':True,'operation_id':'fixture-validation'}
    monkeypatch.setattr(app.state.runtime,'dispatch',pending)
    assert response_error(submit(values),kind)['code']=='VALIDATION_PENDING'
    before=intents(app.state.store)
    assert response_error(submit(values),kind)['operation_id']=='fixture-validation'
    successful=[]
    monkeypatch.setattr(app.state.runtime,'dispatch',validate_ok(successful))
    result=response_value(submit(values),kind)
    assert calls==[successful[0][0]]*2 and before==intents(app.state.store)
    assert response_value(submit(values),kind)['id']==result['id']
    conflict=response_error(submit({**values,'subpath':'another'}),kind)
    assert conflict['code']=='IDEMPOTENCY_CONFLICT'
    assert len(successful)==1


@pytest.mark.parametrize('kind',['panel','mcp'])
def test_explicit_root_folder_reference_can_overlap_without_escaping_source(team, monkeypatch, kind):
    app,b=team;submit=caller(b,kind)
    monkeypatch.setattr(app.state.runtime,'dispatch',validate_ok([]))
    result=response_value(submit(body(subpath='.')),kind)
    row=app.state.store.one('SELECT * FROM projects WHERE id=?',(result['id'],))
    assert row['folder_id']=='folder-team' and row['subpath']=='.'
    assert not {'root','device_id','role_id'}&row.keys()


def test_folder_create_permission_alone_does_not_grant_access_to_created_project(team, monkeypatch):
    app,b=team;submit=caller(b,'mcp',project_access=False)
    monkeypatch.setattr(app.state.runtime,'dispatch',validate_ok([]))
    result=response_value(submit(body()),'mcp')
    assert result['accessible'] is False
    assert app.state.store.one('SELECT folder_id FROM projects WHERE id=?',(result['id'],))['folder_id']=='folder-team'
    assert all('all_projects' not in row['policy'] or not json.loads(row['policy'])['project_rules']
        for row in app.state.store.all("SELECT policy FROM access_profiles"))


def test_update_failure_restores_prior_folder_reference_and_version(team, monkeypatch):
    app,b=team;store=app.state.store;runtime=app.state.runtime;resources=runtime.workspace_resources
    before=store.one("SELECT * FROM projects WHERE id='project-team'")
    original=resources._commit_validated;events=[]
    monkeypatch.setattr(runtime,'dispatch',validate_ok([]))
    monkeypatch.setattr(runtime,'publish',lambda *args:events.append(args))
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise DevError('COMMIT_FIXTURE_FAILURE','retain original mapping',409)
    monkeypatch.setattr(resources,'_commit_validated',fail)
    values=body(alias='Updated',expected_version=before['version'])
    response=b['owner'].put('/api/projects/project-team',json=values)
    assert response.status_code==409 and response.json()['error']['code']=='COMMIT_FIXTURE_FAILURE'
    assert store.one("SELECT * FROM projects WHERE id='project-team'")==before
    assert not [event for event in events if event[0]=='project']
    monkeypatch.setattr(resources,'_commit_validated',original)
    updated=must(b['owner'].put('/api/projects/project-team',json=values))
    assert updated['id']==before['id'] and updated['version']==before['version']+1
