"""Simulated host metadata, durable associations and authenticated history isolation."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub.conversations import validated_url
from hub.store import Store
from shared.crypto import password_hash
from shared.util import DevError
from tests.test_audit_api import api as api
from tests.test_iam_integration import team as team, assign
from tests.test_roles import role, must
from tests.test_resource_access import connection, inventory
from tests.test_mcp_gateway import gw as gw, connector, account, publish


def rpc(client, token, name='project_query', args=None, metadata=None):
    params={'name':name,'arguments':args or {'operation':'list'}}
    if metadata is not None:params['_meta']=metadata
    return client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':params},
        headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream','Content-Type':'application/json'})


def value(response):
    assert response.status_code==200,response.text
    result=response.json()['result'];assert not result.get('isError'),result
    return result['structuredContent']


def records(client,token):
    return value(rpc(client,token,'conversations',{'operation':'list'}))['conversations']


def test_multiple_resources_and_concurrent_conversations_never_share_active_project(api):
    app,client,_=api;testing,production=inventory(app,client)
    from tests.test_continuous_access import add_project
    add_project(app);app.state.store.execute("UPDATE projects SET allow_tasks=1 WHERE id='future'")
    r=role(client,label='worker',project_rules=[{'actions':['read','write','execute'],'all_projects':True}],
           vps_rules=[{'actions':['read','execute'],'vps':[testing['id']]}])
    g=must(connection(client,r,'role'),201)
    cases=[('one','fixture',testing['target']),('two','future','agent'),('one','future','agent'),('two','fixture','agent')]
    def run(item):
        identity,project,target=item
        response=rpc(client,g['token'],'exec',{'project':project,'target':target,'command':'true',
            'yield_seconds':0,'idempotency_key':uuid.uuid4().hex},{'openai/session':identity})
        return value(response)['operation_id']
    with ThreadPoolExecutor(max_workers=4) as executor:ids=list(executor.map(run,cases))
    rows=records(client,g['token']);assert len(rows)==2
    for row in rows:
        detail=value(rpc(client,g['token'],'conversations',{'operation':'get','conversation_id':row['id']}))['conversation']
        assert {x['id'] for x in detail['resources'] if x['type']=='project'}=={'project','future'}
        expected={ids[i] for i,c in enumerate(cases) if c[0]==row['conversation_identifier']}
        assert {x['id'] for x in detail['operations']}==expected
        assert detail['original_url']=='' and detail['platform']=='chatgpt'
    assert app.state.store.one('SELECT count(*) AS n FROM workflows')['n']==0
    assert app.state.store.one('SELECT count(*) AS n FROM workflow_events')['n']==0
    columns={x['name'] for x in app.state.store.all('PRAGMA table_info(conversations)')}
    assert not columns & {'steps','goal','progress','summary','next_action','checkpoints','transcript'}
    assert app.state.runtime.conversations.write_errors==0


@pytest.mark.parametrize('metadata',[None,{}, {'openai/session':None},{'openai/session':42},{'openai/session':''}, {'codepier/conversation':{'platform':'x','role_id':'admin'}}])
def test_absent_or_invalid_metadata_never_fabricates_identity_or_breaks_tools(api,metadata):
    app,client,token=api
    assert value(rpc(client,token,metadata=metadata))['projects']
    assert records(client,token)==[]


def test_other_client_metadata_and_supplied_url_are_independent_of_id(api):
    app,client,token=api
    metadata={'codepier/conversation':{'platform':'custom-client','conversation_identifier':'local-123',
              'label':'Review A','original_url':'https://chat.example.org/conversations/local-123'}}
    value(rpc(client,token,metadata=metadata));row=records(client,token)[0]
    assert row['platform']=='custom-client' and row['conversation_identifier']=='local-123'
    assert row['label']=='Review A' and row['original_url']==metadata['codepier/conversation']['original_url']
    value(rpc(client,token,metadata={'openai/session':'anonymous-value','codepier/conversation':{
        'label':'Testing','original_url':'https://chatgpt.com/c/real-visible-id'}}))
    chat=next(r for r in records(client,token) if r['platform']=='chatgpt')
    assert chat['conversation_identifier']=='anonymous-value' and chat['original_url']=='https://chatgpt.com/c/real-visible-id'
    value(rpc(client,token,metadata={'openai/session':'bad-url','codepier/conversation':{'original_url':'javascript:alert(1)'}}))
    assert next(r for r in records(client,token) if r['conversation_identifier']=='bad-url')['original_url']==''


@pytest.mark.parametrize('url',['javascript:alert(1)','http://chatgpt.com/c/id','https://evil.test/c/id',
    'https://chatgpt.com/','https://user:pass@chatgpt.com/c/id','https://chatgpt.com:444/c/id','https://chatgpt.com\\@evil.test/c/id'])
def test_invalid_explicit_chatgpt_urls_rejected_atomically(api,url):
    app,client,_=api
    response=client.post('/api/conversations',json={'platform':'chatgpt','conversation_identifier':'supplied',
        'original_url':url,'resources':[{'type':'project','id':'project'}]})
    assert response.status_code==400,response.text
    assert app.state.store.one('SELECT count(*) AS n FROM conversations')['n']==0


@pytest.mark.parametrize('url', ['https://a..example.org/c/id', 'https://-bad.example.org/c/id',
    'https://bad_host.example.org/c/id', 'https://127.0.0.1/c/id', 'https://[::1]/c/id'])
def test_other_client_urls_reject_invalid_dns_and_local_addresses(api, url):
    app, client, _ = api
    response = client.post('/api/conversations', json={'platform': 'custom-client',
        'conversation_identifier': 'real-supplied-id', 'original_url': url})
    assert response.status_code == 400, response.text
    assert app.state.store.one('SELECT count(*) AS n FROM conversations')['n'] == 0


def test_conversation_http_identifiers_have_bounded_validation(api):
    _, client, _ = api
    assert client.get('/api/conversations', params={'resource_type': 'project', 'resource_id': 'p' * 101}).status_code == 422
    assert client.get('/api/conversations/' + 'c' * 101).status_code == 422


def test_manual_index_can_add_url_and_resources_but_never_progress_fields(api):
    app,client,_=api
    created=must(client.post('/api/conversations',json={'platform':'custom','conversation_identifier':'supplied-id',
        'resources':[{'type':'project','id':'project'}]}),201)['conversation']
    assert created['owner_user_id']=='owner' and created['grant_id'] is None
    edited=must(client.put('/api/conversations/'+created['id'],json={'platform':'custom','conversation_identifier':'supplied-id',
        'label':'A label','original_url':'https://client.example/c/supplied-id'}))['conversation']
    assert edited['original_url'].endswith('supplied-id') and edited['resources']==created['resources']
    for key in ('goal','steps','progress','summary','next_actions','transcript'):
        response=client.post('/api/conversations',json={'platform':'custom','conversation_identifier':'a',key:'forbidden'})
        assert response.status_code==422,response.text
    assert client.put('/api/conversations/'+created['id'],json={'platform':'custom','conversation_identifier':'replacement'}).status_code==409


def test_same_id_and_profile_never_cross_grant_user_or_space_histories(team):
    app,b=team;r=role(b['owner'],label='reviewer',project_rules=[{'actions':['read'],'projects':['project-team']}])
    for uid in ('alice','bob'):assign(b['owner'],r,uid)
    alice=must(connection(b['alice'],r,'role'),201)
    # Explicitly reusing a stable Profile still creates a separate private grant.
    profile=must(b['alice'].get('/api/access-profiles/'+alice['profile_id']))
    other=must(connection(b['alice'],r,'role',profile_id=profile['id'],profile_version=profile['version']),201)
    bob=must(connection(b['bob'],r,'role'),201)
    for client,grant in [(b['alice'],alice),(b['alice'],other),(b['bob'],bob)]:
        value(rpc(client,grant['token'],metadata={'openai/session':'identical'}))
    first=records(b['alice'],alice['token'])[0]
    second=records(b['alice'],other['token'])[0]
    assert first['id']!=second['id'] and first['profile_id']==second['profile_id']
    denied=rpc(b['alice'],other['token'],'conversations',{'operation':'get','conversation_id':first['id']})
    assert denied.json()['result']['structuredContent']['error']['code']=='CONVERSATION_NOT_FOUND'
    assert b['bob'].get('/api/conversations/'+first['id']).status_code==404
    assert b['owner'].get('/api/conversations/'+first['id']).status_code==404
    assert b['alice'].get('/api/conversations/'+first['id'],headers={'X-CodePier-Space':profile.get('space_id','legacy')}).status_code in (403,404)
    assert len(must(b['alice'].get('/api/conversations'))['conversations'])==2
    assert b['alice'].post('/api/conversations',json={'platform':'custom','conversation_identifier':'cross-space',
        'resources':[{'type':'project','id':'project-legacy'}]}).status_code==404


def test_conversations_survive_actual_hub_close_reopen_and_preserve_receipt(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL','http://testserver');monkeypatch.setenv('MCP_PUBLIC_URL','')
    directory=tmp_path/'hub';app=create_app(str(directory));store=app.state.store
    store.execute('INSERT INTO users VALUES (?,?,?,?)',('owner','admin',password_hash('fixture-only'),time.time()))
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',('d','Fixture',store.encrypt('fixture'),time.time()))
    store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created) VALUES (?,?,?,?,?,?,?,?)',('p','P','p','d',str(tmp_path/'p'),'write',1,time.time()))
    from hub.principal import Principal
    grant=app.state.auth.issue_grant(Principal('panel:admin','owner',{'read','execute'},[],admin=True),'client',['read','execute'],['p'])
    key=uuid.uuid4().hex
    with TestClient(app) as client:
        args={'project':'P','command':'true','yield_seconds':0,'idempotency_key':key}
        operation=value(rpc(client,grant['token'],'exec',args,{'openai/session':'persistent'}))['operation_id']
        original=records(client,grant['token'])[0]
    app2=create_app(str(directory))
    with TestClient(app2) as client:
        restored=records(client,grant['token'])[0]
        assert restored['id']==original['id'] and restored['first_activity']==original['first_activity']
        assert restored['original_url']==''
        detail=value(rpc(client,grant['token'],'conversations',{'operation':'get','conversation_id':restored['id']}))['conversation']
        assert detail['operations'][0]['id']==operation
        assert value(rpc(client,grant['token'],'exec',args,{'openai/session':'persistent'}))['operation_id']==operation
        assert app2.state.store.one('SELECT count(*) AS n FROM operations')['n']==1
        assert app2.state.store.one('SELECT count(*) AS n FROM workflows')['n']==0


def test_remote_mcp_calls_correlate_without_exposing_other_account(gw):
    app,b,backend=gw;c=connector(b);a=account(b,c);binding=publish(b,a)
    r=role(b['owner'],label='reviewer',project_rules=[],connector_rules=[{'binding_id':binding['id'],'tools':['echo']}])
    assign(b['owner'],r,'alice');g=must(connection(b['alice'],r,'role',confirm_external_mcp=True),201)
    response=rpc(b['alice'],g['token'],'kiln__echo',{'value':'fixture'},{'openai/session':'remote'})
    assert not response.json()['result'].get('isError')
    row=records(b['alice'],g['token'])[0]
    detail=value(rpc(b['alice'],g['token'],'conversations',{'operation':'get','conversation_id':row['id']}))['conversation']
    assert detail['resources']==[{'type':'mcp','id':binding['id'],'name':'Fixture / kiln'}]
    assert detail['operations'][0]['type']=='mcp' and detail['operations'][0]['tool']=='echo'
    assert app.state.runtime.conversations.write_errors==0
