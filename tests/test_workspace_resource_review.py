"""Issue 11 review regressions at real HTTP, authorization and SQLite boundaries."""
import asyncio
import json
import time
import uuid

import pytest

from hub import iam
from hub.principal import Principal
from hub.workspace_api import PoolSave, ProjectSave, SourceSave
from hub.workspace_sources import resolve_shell, select_view, source_views
from shared.util import DevError
from shared.workspace_contracts import SourceInput
from tests.current_model import CurrentModel
from tests.test_filesystem_sources import resources  # noqa: F401
from tests.test_workspace_http import web  # noqa: F401


def source_request(client, identifier='folder', **changes):
    response=client.get('/api/spaces/alice/sources')
    assert response.status_code==200,response.text
    source=next(row for row in response.json()['sources'] if row['id']==identifier)
    return {**{key:source[key] for key in SourceInput.model_fields},
            'enabled':bool(source['enabled']),'allow_tasks':bool(source['allow_tasks']),
            'expected_version':source['version'],'idempotency_key':uuid.uuid4().hex,**changes}


def login(client, user):
    response=client.post('/api/login',json={'username':user,'password':'a-valid-test-password'})
    assert response.status_code==200,response.text
    client.headers['X-RD-CSRF']=response.json()['csrf']


def validation_stub(app, monkeypatch):
    calls=[]
    async def validate(tool,args,project,principal,**kwargs):
        assert tool=='system_validate'
        app.state.runtime.workspace_resources.require_validation(principal,project,tool)
        calls.append((principal.space_id,project['device_id'],project['root']))
        return {'root':project['root'],'writable':True,'allow_tasks':True}
    monkeypatch.setattr(app.state.runtime,'dispatch',validate)
    return calls


@pytest.mark.parametrize('broken',['offline','pool','mapping','device','folder'])
def test_deactivation_needs_no_runner_or_resolvable_dependencies(web,monkeypatch,tmp_path,broken):
    client,app=web;store=app.state.store
    identifier='folder'
    if broken=='folder':
        identifier=CurrentModel(store).folder('direct',space='alice',device='a',path=tmp_path)
        store.execute("UPDATE projects SET source_id='direct' WHERE id='project'")
    body=source_request(client,identifier,enabled=False)
    if broken=='pool':store.execute("UPDATE storage_pools SET enabled=0 WHERE id='pool'")
    elif broken=='mapping':store.execute("UPDATE storage_mappings SET enabled=0 WHERE id='map_b'")
    elif broken=='device':store.execute("UPDATE devices SET enabled=0 WHERE id='a'")
    elif broken=='folder':store.execute("UPDATE folders SET enabled=0 WHERE id='direct'")
    async def forbidden(*args,**kwargs):
        pytest.fail('Deactivation must not dispatch Runner validation or work')
    monkeypatch.setattr(app.state.runtime,'dispatch',forbidden)
    before=store.one('SELECT * FROM space_sources WHERE id=?',(identifier,))
    principal=Principal('panel:alice','alice',{'read'},[],space_id='alice',user_epoch=1)
    snapshot=iam.panel_access_snapshot(store,principal)
    response=client.put('/api/spaces/alice/sources/'+identifier,json=body)
    assert response.status_code==200,response.text
    assert not response.json()['enabled'] and not response.json().get('replayed',False)
    after=store.one('SELECT * FROM space_sources WHERE id=?',(identifier,))
    assert after=={**before,'enabled':0,'version':before['version']+1}
    assert not snapshot <= iam.panel_access_snapshot(store,principal)
    replay=client.put('/api/spaces/alice/sources/'+identifier,json=body)
    assert replay.status_code==200,replay.text
    assert replay.json()=={**response.json(),'replayed':True}
    assert store.one('SELECT * FROM space_sources WHERE id=?',(identifier,))==after
    for tool,args in [('fs_tree',{'project':'project'}),
                      ('exec',{'project':'project','command':'printf must-not-run','idempotency_key':uuid.uuid4().hex})]:
        with pytest.raises(DevError) as denied:
            asyncio.run(app.state.runtime.invoke(tool,args,principal))
        assert denied.value.code=='PROJECT_SOURCE_UNAVAILABLE'
    assert store.one('SELECT count(*) AS n FROM operations')['n']==0


@pytest.mark.parametrize('change',[
    {'subpath':'elsewhere'}, {'mapping_ids':['map_a']},
    {'files':{'mode':'custom','device_id':'a'}},
    {'shell':{'mode':'custom','default_device_id':'b'}},
    {'mode':'read','allow_tasks':False}, {'name':'also-renamed'},
])
def test_deactivation_cannot_persist_simultaneous_configuration_changes(web,monkeypatch,change):
    client,app=web;store=app.state.store
    body=source_request(client,enabled=False,**change)
    async def forbidden(*args,**kwargs):
        pytest.fail('Mixed deactivation must be rejected before Runner dispatch')
    monkeypatch.setattr(app.state.runtime,'dispatch',forbidden)
    before=store.one("SELECT * FROM space_sources WHERE id='folder'")
    response=client.put('/api/spaces/alice/sources/folder',json=body)
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='SOURCE_DEACTIVATION_ONLY'
    assert store.one("SELECT * FROM space_sources WHERE id='folder'")==before
    assert not store.one('SELECT receipt FROM workspace_requests WHERE request_key=?',(body['idempotency_key'],))


@pytest.mark.parametrize('denial',['stale','membership','other_account'])
def test_deactivation_keeps_version_and_live_authority_checks(web,monkeypatch,denial):
    client,app=web;store=app.state.store
    body=source_request(client,enabled=False)
    before=store.one("SELECT * FROM space_sources WHERE id='folder'")
    if denial=='stale':body['expected_version']=0
    elif denial=='membership':store.execute("UPDATE memberships SET policy='{}' WHERE space_id='alice' AND user_id='alice'")
    else:login(client,'bob')
    calls=validation_stub(app,monkeypatch)
    response=client.put('/api/spaces/alice/sources/folder',json=body)
    assert response.status_code==(409 if denial=='stale' else 403),response.text
    assert calls==[] and store.one("SELECT * FROM space_sources WHERE id='folder'")==before


def test_reenable_still_validates_and_pending_keys_cannot_be_repurposed(web,monkeypatch):
    client,app=web
    calls=validation_stub(app,monkeypatch)
    disabled=client.put('/api/spaces/alice/sources/folder',json=source_request(client,enabled=False))
    assert disabled.status_code==200,disabled.text
    assert calls==[]
    enabled=client.put('/api/spaces/alice/sources/folder',json=source_request(client,enabled=True,subpath='validated-change'))
    assert enabled.status_code==200,enabled.text
    assert {device for _,device,_ in calls}=={'a','b'}
    pending_body=source_request(client,subpath='pending-change')
    async def pending(*args,**kwargs):
        return {'pending':True,'operation_id':'a'*32}
    monkeypatch.setattr(app.state.runtime,'dispatch',pending)
    first=client.put('/api/spaces/alice/sources/folder',json=pending_body)
    assert first.status_code==409 and first.json()['error']['code']=='VALIDATION_PENDING'
    attempted=source_request(client,enabled=False,idempotency_key=pending_body['idempotency_key'])
    denied=client.put('/api/spaces/alice/sources/folder',json=attempted)
    assert denied.status_code==409 and denied.json()['error']['code']=='VALIDATION_CHANGED'
    assert app.state.store.one("SELECT enabled FROM space_sources WHERE id='folder'")['enabled']==1


@pytest.mark.asyncio
async def test_deactivation_preserves_running_receipt_and_original_target(resources):
    ws,service,calls=resources
    row=service.source_public(ws.store.one("SELECT * FROM space_sources WHERE id='folder'"))
    body=SourceSave.model_validate({**{k:row[k] for k in SourceInput.model_fields},'enabled':False,'allow_tasks':bool(row['allow_tasks']),
        'expected_version':row['version'],'idempotency_key':uuid.uuid4().hex})
    identifier=uuid.uuid4().hex
    ws.store.execute('''INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,fingerprint,state,
        created,updated,space_id,owner_user_id,output,route_context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (identifier,'a','project','panel:alice','routed_exec','{}','original','running',time.time(),time.time(),
         'alice','alice','original output',json.dumps({'original_device':'a','original_source':'folder'})))
    original=ws.store.one('SELECT * FROM operations WHERE id=?',(identifier,))
    await service.validated_save('source',ws.principal,body,'folder')
    await service.validated_save('source',ws.principal,body,'folder')
    assert ws.store.one('SELECT * FROM operations WHERE id=?',(identifier,))==original
    assert calls==[]


@pytest.mark.parametrize('kind',['pool_create','folder_create','folder_update'])
def test_global_inventory_http_replay_returns_original_receipt_without_validation(web,monkeypatch,kind):
    client,app=web;store=app.state.store
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    calls=validation_stub(app,monkeypatch)
    if kind=='pool_create':
        path='/api/storage-pools';method='POST';table='storage_pools'
        body={'name':'Replay pool','abstract_base':'/storage/replay','default_fs_device_id':'a',
              'default_shell_device_id':'b','mappings':[{'device_id':d,'path':'/mnt/'+d} for d in ('a','b')],
              'confirmed_same_storage':True,'idempotency_key':uuid.uuid4().hex}
    else:
        path='/api/folders';method='POST';table='folders'
        body={'name':'Replay folder','device_id':'a','device_path':'/mnt/a/replay','mode':'write',
              'allow_tasks':True,'idempotency_key':uuid.uuid4().hex}
        if kind=='folder_update':
            initial=client.post(path,json=body)
            assert initial.status_code==201,initial.text
            path+='/'+initial.json()['id'];method='PUT'
            body={**body,'name':'Updated folder','expected_version':initial.json()['version'],'idempotency_key':uuid.uuid4().hex}
    first=client.request(method,path,json=body)
    assert first.status_code in {200,201},first.text
    saved=first.json();before=list(calls)
    count=store.one('SELECT count(*) AS n FROM '+table)['n']
    again=client.request(method,path,json=body)
    assert again.status_code==first.status_code,again.text
    assert again.json()=={**saved,'replayed':True}
    assert calls==before and all(space is None for space,_,_ in calls)
    assert store.one('SELECT count(*) AS n FROM '+table)['n']==count
    assert store.one('SELECT version FROM '+table+' WHERE id=?',(saved['id'],))['version']==saved['version']
    conflict=client.request(method,path,json={**body,'name':'Changed payload'})
    assert conflict.status_code==409 and conflict.json()['error']['code']=='IDEMPOTENCY_CONFLICT'
    login(client,'bob')
    denied=client.request(method,path,json=body)
    assert denied.status_code==(409 if kind=='pool_create' else 404),denied.text
    if kind=='pool_create':assert denied.json()['error']['code']=='ABSTRACT_PATH_CONFLICT'
    assert calls==before and store.one('SELECT count(*) AS n FROM '+table)['n']==count
    assert not store.one("SELECT receipt FROM workspace_requests WHERE user_id='bob' AND request_key=?",(body['idempotency_key'],))
    login(client,'alice')
    store.execute('UPDATE '+table+" SET owner_user_id='bob' WHERE id=?",(saved['id'],))
    revoked=client.request(method,path,json=body)
    assert revoked.status_code==404,revoked.text
    assert calls==before


@pytest.mark.parametrize('enabled',[True,False])
def test_source_receipt_replay_requires_current_workspace_management(web,monkeypatch,enabled):
    client,app=web
    calls=validation_stub(app,monkeypatch)
    body=source_request(client,enabled=enabled)
    first=client.put('/api/spaces/alice/sources/folder',json=body)
    assert first.status_code==200,first.text
    before=list(calls)
    replay=client.put('/api/spaces/alice/sources/folder',json=body)
    assert replay.status_code==200 and replay.json()=={**first.json(),'replayed':True}
    app.state.store.execute("UPDATE memberships SET policy='{}' WHERE space_id='alice' AND user_id='alice'")
    denied=client.put('/api/spaces/alice/sources/folder',json=body)
    assert denied.status_code==403 and denied.json()['error']['code']=='WORKSPACE_ADMIN_REQUIRED'
    assert calls==before


def test_deactivation_rolls_back_if_receipt_audit_cannot_commit(web,monkeypatch):
    client,app=web;store=app.state.store
    body=source_request(client,enabled=False)
    before=store.one("SELECT * FROM space_sources WHERE id='folder'")
    events=[]
    monkeypatch.setattr(app.state.runtime,'publish',lambda *args:events.append(args))
    original=iam.audit
    def fail_audit(store_,principal,action,*args,**kwargs):
        if action=='source.save:folder':raise DevError('AUDIT_FIXTURE_FAILURE','rollback deactivation',409)
        return original(store_,principal,action,*args,**kwargs)
    monkeypatch.setattr(iam,'audit',fail_audit)
    response=client.put('/api/spaces/alice/sources/folder',json=body)
    assert response.status_code==409 and response.json()['error']['code']=='AUDIT_FIXTURE_FAILURE'
    assert store.one("SELECT * FROM space_sources WHERE id='folder'")==before
    assert not store.one('SELECT receipt FROM workspace_requests WHERE request_key=?',(body['idempotency_key'],))
    assert not [event for event in events if event[0]=='project']


@pytest.mark.parametrize('project_mode',['custom','disabled'])
def test_project_overrides_cannot_hide_invalid_inherited_source_default(resources,project_mode):
    ws,service,_=resources
    settings={'mode':project_mode,**({'default_device_id':'b'} if project_mode=='custom' else {})}
    for project in ('project','other'):
        ws.store.execute('INSERT INTO project_shell_settings(project_id,settings) VALUES(?,?)',(project,json.dumps(settings)))
    before=ws.store.one("SELECT * FROM storage_pools WHERE id='pool'")
    body=PoolSave(name=before['name'],abstract_base=before['abstract_base'],default_fs_device_id='b',
        default_shell_device_id='c',expected_version=before['version'],idempotency_key=uuid.uuid4().hex)
    with pytest.raises(DevError) as denied:
        service.save_pool(ws.principal,body,'pool')
    assert denied.value.code=='WORKSPACE_CONFIGURATION_CONFLICT'
    assert ws.store.one("SELECT * FROM storage_pools WHERE id='pool'")==before
    assert not ws.store.one('SELECT receipt FROM workspace_requests WHERE request_key=?',(body.idempotency_key,))
    assert set(source_views(ws.store,ws.project)['views'])=={'a','b'}


@pytest.mark.asyncio
@pytest.mark.parametrize('source_mode',['custom','disabled'])
async def test_valid_source_override_or_explicit_disable_preserves_root_inheritance(resources,source_mode):
    ws,service,_=resources
    settings={'mode':source_mode,**({'default_device_id':'b'} if source_mode=='custom' else {})}
    ws.store.execute("UPDATE space_sources SET shell=? WHERE id='folder'",(json.dumps(settings),))
    for project in ('project','other'):
        ws.store.execute('INSERT INTO project_shell_settings(project_id,settings) VALUES(?,?)',
            (project,json.dumps({'mode':'custom','default_device_id':'b'})))
    service.save_pool(ws.principal,PoolSave(name='Shared',abstract_base='/storage/shared',
        default_fs_device_id='b',default_shell_device_id='c',expected_version=1,idempotency_key=uuid.uuid4().hex),'pool')
    project=await service.validated_save('project',ws.principal,ProjectSave(alias='NewRoot',source_id='folder',
        subpath='.',idempotency_key=uuid.uuid4().hex))
    assert set(source_views(ws.store,project)['views'])=={'a','b'}
    assert select_view(ws.store,project)['device_id']=='b'
    if source_mode=='custom':assert resolve_shell(ws.store,project)['default_device_id']=='b'
    else:
        with pytest.raises(DevError) as denied:resolve_shell(ws.store,project)
        assert denied.value.code=='SHELL_DISABLED'
    assert not ws.store.all('SELECT * FROM space_devices')
