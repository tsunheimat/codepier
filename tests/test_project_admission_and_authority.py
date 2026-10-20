"""Current Project/Folder admission and cutoff at actual HTTP/SQLite boundaries.

No real device is touched. Synthetic completion supplies the original queued
validation response; auth and commit paths are the actual application code.
"""
import json
from pathlib import Path
import uuid

import pytest

from tests.current_model import CurrentModel
from tests.test_audit_api import api, rpc  # noqa: F401

SPACE='/api/spaces/audit-space'


def mapping(**changes):
    return {'alias':'NewMapping','folder_id':'audit-folder','subpath':'candidate',
        'idempotency_key':uuid.uuid4().hex,**changes}


def test_slow_validation_reuses_actual_operation_and_commits_only_once(api):
    app,client,_=api;body=mapping();store=app.state.store
    path=SPACE+'/projects'
    first=client.post(path,json=body);second=client.post(path,json=body)
    assert first.status_code==second.status_code==409
    one,two=first.json()['error'],second.json()['error']
    assert one['code']==two['code']=='VALIDATION_PENDING'
    assert one['operation_id']==two['operation_id']
    assert store.one("SELECT count(*) AS n FROM operations WHERE tool='system_validate'")['n']==1
    assert not store.one("SELECT id FROM projects WHERE alias='NewMapping'")
    original=store.one('SELECT payload FROM operations WHERE id=?',(one['operation_id'],))
    request=json.loads(store.decrypt(original['payload']))
    root=str(Path(store.one("SELECT device_path FROM space_folders WHERE id='audit-folder'")['device_path'])/'candidate')
    assert request['project']['root']==root
    store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?",(
        json.dumps({'ok':True,'data':{'root':root,'writable':True,'allow_tasks':False}}),one['operation_id']))
    done=client.post(path,json=body);again=client.post(path,json=body)
    assert done.status_code==again.status_code==201,(done.text,again.text)
    assert done.json()['id']==again.json()['id']
    assert store.one("SELECT count(*) AS n FROM projects WHERE alias='NewMapping'")['n']==1
    assert store.one("SELECT count(*) AS n FROM operations WHERE tool='system_validate'")['n']==1
    conflict=client.post(path,json={**body,'subpath':'different'})
    assert conflict.status_code==409 and conflict.json()['error']['code']=='IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('change',['project','alias','device','login'])
def test_validation_cannot_commit_after_authority_or_target_changes(api,monkeypatch,change):
    app,client,_=api;store=app.state.store
    current=store.one("SELECT * FROM projects WHERE id='project'")
    body=mapping(alias='fixture' if change=='project' else 'NewMapping')
    if change=='project':body['expected_version']=current['version']
    async def validation(name,args,project,principal,**kwargs):
        assert name=='system_validate'
        if change=='project':
            store.execute("UPDATE projects SET description='newer window',version=version+1 WHERE id='project'")
        elif change=='alias':
            CurrentModel(store).project('other-window',space='audit-space',folder='audit-folder',
                owner='owner',alias='NewMapping',subpath='elsewhere')
        elif change=='device':store.execute("UPDATE devices SET enabled=0 WHERE id='device'")
        else:store.execute('DELETE FROM sessions')
        return {'root':project['root'],'writable':True,'allow_tasks':False}
    monkeypatch.setattr(app.state.runtime,'dispatch',validation)
    response=(client.put('/api/projects/project',json=body) if change=='project'
        else client.post(SPACE+'/projects',json=body))
    assert response.status_code in {401,409},response.text
    expected={'project':'VERSION_CHANGED','alias':'ALIAS_EXISTS',
        'device':'SOURCE_DEVICE_UNAVAILABLE','login':'UNAUTHENTICATED'}
    if change=='login':assert response.status_code==401
    else:assert response.json()['error']['code']==expected[change]
    if change=='project':
        saved=store.one("SELECT * FROM projects WHERE id='project'")
        assert saved['description']=='newer window' and saved['subpath']==current['subpath']
    else:assert not store.one("SELECT id FROM projects WHERE subpath='candidate'")
    assert not store.one('SELECT request_key FROM workspace_requests WHERE request_key=?',(body['idempotency_key'],))


def test_key_rotation_preserves_user_selected_agent_url(api):
    _app,client,_=api;selected='https://agent-facing.fixture:9443'
    created=client.post('/api/devices',json={'name':'Different route','hub_url':selected})
    assert created.status_code==200,created.text
    original=created.json()['pairing']
    rotated=client.post('/api/devices/'+original['device_id']+'/rotate')
    assert rotated.status_code==200,rotated.text
    assert rotated.json()['pairing']['hub_url']==selected
    assert rotated.json()['pairing']['secret']!=original['secret']


@pytest.mark.parametrize('uri',['ui://relay/workspace-v1.html','ui://relay/changes-v1.html',
    'ui://codepier/workspace-v1.html'])
def test_retired_app_resource_is_rejected_without_redirect_or_readable_notice(api,uri):
    _app,client,pat=api
    response=rpc(client,pat,{'jsonrpc':'2.0','id':1,'method':'resources/read','params':{'uri':uri}})
    assert response.status_code==200
    result=response.json()
    assert result['error']['code']==-32002 and 'result' not in result
    assert 'location' not in response.headers


def test_browser_offline_is_rejected_before_an_operation_exists(api,monkeypatch):
    app,client,_=api
    # A source may allow execution while the Runner is offline. Bypass only
    # browser-provider discovery in this fixture, not identity or admission.
    app.state.store.execute("UPDATE space_folders SET allow_tasks=1 WHERE id='audit-folder'")
    app.state.store.execute("UPDATE devices SET info=? WHERE id='device'",(json.dumps({'shell_enabled':True}),))
    monkeypatch.setattr(app.state.runtime.integrations,'guard',lambda *_a,**_k:None)
    response=client.post('/api/tools/call',json={'tool':'browser_open','arguments':{
        'project':'project','url':'https://example.test/','idempotency_key':'offline-fixture-only'}})
    assert response.status_code==409,response.text
    error=response.json()['error']
    assert error['code']=='COMPUTER_OFFLINE' and error['admitted'] is False
    assert not error.get('operation_id')
    assert not app.state.store.one("SELECT id FROM operations WHERE idem='offline-fixture-only'")
