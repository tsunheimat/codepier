"""Consolidated setup proves real action/resource checks on isolated Hub data."""
import json
import time
import uuid

import pytest

from tests.test_audit_api import api as api
from tests.test_roles import role, update_role, must, principal
from tests.test_access_profiles import call, data
from tests.test_continuous_access import add_project
from tests.test_mcp_gateway import gw as gw, connector, account, publish
from tests.test_iam_integration import team as team, assign
from hub.roles import role_project_scopes


def connection(client, r, mode='fixed', **extra):
    return client.post('/api/client-connections',json={
        'name':'Worker client','role_id':r['id'],'expected_role_version':r['version'],
        'authorization_mode':mode,'confirm_dynamic_role':mode == 'role',
        'idempotency_key':uuid.uuid4().hex, **extra})


def inventory(app, client):
    app.state.store.execute("UPDATE projects SET allow_tasks=1 WHERE id='project'")
    rows=[]
    for name in ('Testing','Production'):
        rows.append(must(client.post('/api/vps',json={'name':name,'host':name.lower()+'.invalid',
            'password':'ISOLATED_SSH_SECRET','project_ids':['project'],'execution_project_id':'project'})))
    return rows


def result_error(response, code):
    value=response.json()['result'];assert value.get('isError'),value
    error=value.get('structuredContent') or json.loads(value['content'][0]['text'])
    assert error['error']['code']==code,error


def execute(client, secret, vps, project='fixture', key=None):
    return call(client,secret,'exec',{'project':project,'target':vps['target'],
        'command':'printf isolated-test','yield_seconds':0,'idempotency_key':key or uuid.uuid4().hex})


@pytest.mark.parametrize('mode',['fixed','role'])
def test_worker_project_write_execute_testing_only(api,mode):
    app,client,legacy=api;testing,production=inventory(app,client);add_project(app)
    r=role(client,label='worker',project_rules=[{'actions':['read','write','execute'],'projects':['project']},
        {'actions':['read'],'projects':['future']}],vps_rules=[{'actions':['read','execute'],'vps':[testing['id']]},
        {'actions':['read'],'vps':[production['id']]}])
    g=must(connection(client,r,mode),201)
    assert role_project_scopes(app.state.store,principal(app,g['token']),'future')=={'read'}
    receipt=data(execute(client,g['token'],testing));assert receipt['operation_id']
    result_error(execute(client,g['token'],production),'VPS_POLICY_DENIED')
    result_error(call(client,g['token'],'exec',{'project':'future','command':'true','idempotency_key':uuid.uuid4().hex}),'ROLE_POLICY_DENIED')
    # Associations never authorize a legacy grant or a metadata-only VPS rule.
    assert data(call(client,legacy,'vps'))['vps']==[]
    result_error(execute(client,legacy,testing),'INSUFFICIENT_SCOPE')
    rows=must(client.get('/api/client-connections'))['connections'];record=next(x for x in rows if x['id']==g['grant_id'])
    assert record['role']['label']=='worker'
    permissions={v['name']:v['actions'] for v in record['permissions']['vps']}
    assert permissions=={'Production':['read'],'Testing':['execute','read']}
    assert 'ISOLATED_SSH_SECRET' not in json.dumps(rows)
    operation=app.state.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    request=json.loads(app.state.store.decrypt(operation['payload']))
    assert app.state.runtime.permission_error(operation,request) is None
    r=must(update_role(client,r,vps_rules=[{'actions':['read'],'vps':[testing['id'],production['id']]}]))
    assert app.state.runtime.permission_error(operation,request)
    result_error(execute(client,g['token'],testing),'VPS_POLICY_DENIED')
    assert client.get('/api/operations/'+receipt['operation_id']).status_code==200  # Owner's independent panel rights.


@pytest.mark.parametrize('mode',['fixed','role'])
def test_fixed_snapshot_and_explicit_dynamic_future_consent(api,mode):
    app,client,_=api;testing,production=inventory(app,client)
    r=role(client,label='generic role',project_rules=[{'actions':['read'],'projects':['project']}],
           vps_rules=[{'actions':['read'],'vps':[testing['id']]}])
    g=must(connection(client,r,mode),201);before=app.state.store.one('SELECT * FROM grants WHERE id=?',(g['grant_id'],))
    add_project(app)
    r=must(update_role(client,r,project_rules=[{'actions':['read','write','execute'],'all_projects':True}],
        vps_rules=[{'actions':['read','execute'],'vps':[testing['id'],production['id']]}]))
    ctx=data(call(client,g['token'],'get_access_context'))
    assert {p['id'] for p in ctx['projects']}==({'project','future'} if mode=='role' else {'project'})
    assert role_project_scopes(app.state.store,principal(app,g['token']),'project')==({'read','write','execute'} if mode=='role' else {'read'})
    assert {v['id'] for v in data(call(client,g['token'],'vps'))['vps']}==({testing['id'],production['id']} if mode=='role' else {testing['id']})
    assert app.state.store.one('SELECT * FROM grants WHERE id=?',(g['grant_id'],))==before
    must(update_role(client,r,enabled=False))
    if mode == 'role': result_error(call(client,g['token'],'vps'),'ROLE_POLICY_DENIED')
    else: assert data(call(client,g['token'],'vps'))['vps']==[]
    status=next(x for x in must(client.get('/api/client-connections'))['connections'] if x['id']==g['grant_id'])
    assert status['status']=='disabled' and not status['permissions']['projects']


def test_creation_consent_version_and_request_key_are_transactional(api):
    app,client,_=api;r=role(client,label='reviewer')
    count=app.state.store.one('SELECT count(*) AS n FROM access_profiles')['n']
    response=connection(client,r,'role',confirm_dynamic_role=False)
    assert response.status_code==400 and response.json()['error']['code']=='DYNAMIC_CONSENT_REQUIRED'
    assert app.state.store.one('SELECT count(*) AS n FROM access_profiles')['n']==count
    assert connection(client,r,expected_role_version=r['version']+1).status_code==409
    key=uuid.uuid4().hex;g=must(connection(client,r,idempotency_key=key),201)
    replay=must(connection(client,r,idempotency_key=key),201)
    assert replay['grant_id']==g['grant_id'] and replay['replayed'] and 'token' not in replay
    assert connection(client,r,idempotency_key=key,name='Different').status_code==409


@pytest.mark.parametrize('end',['expiry','revoke','profile'])
def test_expired_revoked_disabled_connections_reject_real_calls(api,end):
    app,client,_=api;r=role(client,label='reviewer');g=must(connection(client,r),201)
    assert 'projects' in data(call(client,g['token'],'get_access_context'))
    if end=='expiry':app.state.store.execute('UPDATE tokens SET expires=? WHERE grant_id=?',(time.time()-1,g['grant_id']))
    elif end=='revoke':must(client.delete('/api/grants/'+g['grant_id']))
    else:app.state.store.execute('UPDATE access_profiles SET enabled=0 WHERE id=?',(g['profile_id'],))
    response=call(client,g['token'],'get_access_context');assert response.status_code==401


def test_execution_route_is_explicit_validated_and_not_association(api):
    app,client,_=api;testing,production=inventory(app,client)
    no_route=must(client.post('/api/vps',json={'name':'Metadata','host':'meta.invalid','password':'fixture','project_ids':['project']}))
    r=role(client,label='worker',project_rules=[{'actions':['read','execute'],'projects':['project']}],
           vps_rules=[{'actions':['read','execute'],'vps':[no_route['id']]}])
    g=must(connection(client,r,'role'),201)
    result_error(execute(client,g['token'],no_route),'VPS_ROUTE_REQUIRED')
    invalid=client.post('/api/vps',json={'name':'Invalid','host':'invalid.invalid','password':'fixture','execution_project_id':'missing'})
    assert invalid.status_code==403
    add_project(app)
    assert client.post('/api/vps',json={'name':'Read route','host':'read.invalid','password':'fixture','execution_project_id':'future'}).status_code==403
    # Route choice need not equal the purpose association.
    app.state.store.execute('DELETE FROM vps_projects WHERE vps_id=?',(testing['id'],))
    r=must(update_role(client,r,vps_rules=[{'actions':['read','execute'],'vps':[testing['id']]}]))
    assert data(execute(client,g['token'],testing))['operation_id']


def test_resource_read_and_use_never_allow_resource_or_authorization_admin(team):
    app,b=team
    r=role(b['owner'],label='manager',project_rules=[{'actions':['read','write','execute'],'projects':['project-team']}])
    assign(b['owner'],r,'alice')
    g=must(connection(b['alice'],r,'role'),201)
    assert b['alice'].post('/api/vps',json={'name':'Forbidden','host':'a.invalid','password':'x'}).status_code==403
    assert b['alice'].post('/api/access-roles',json={'label':'admin','idempotency_key':uuid.uuid4().hex}).status_code==403
    assert b['alice'].get('/api/client-connections').status_code==200
    assert g['grant_id'] not in b['bob'].get('/api/client-connections').text
    assert b['alice'].get('/api/resources/project/project-legacy').status_code==404
    assert connection(b['bob'],r,'role').status_code==403


@pytest.mark.parametrize('mode',['fixed','role'])
def test_mcp_binding_tools_accounts_and_role_change_are_real_checks(gw,mode):
    app,b,backend=gw;c=connector(b);a=account(b,c);binding=publish(b,a)
    other=account(b,c,who='bob');other_binding=publish(b,other,who='bob',alias='bob')
    r=role(b['owner'],label='worker',project_rules=[],connector_rules=[{'binding_id':binding['id'],'tools':['echo']}])
    assign(b['owner'],r,'alice')
    g=must(connection(b['alice'],r,mode,confirm_external_mcp=True),201)
    first=call(b['alice'],g['token'],'kiln__echo',{'value':'permitted'})
    assert not first.json()['result'].get('isError') and backend.effects==1
    result_error(call(b['alice'],g['token'],'kiln__run',{}),'UNKNOWN_TOOL')
    result_error(call(b['alice'],g['token'],'bob__echo',{'value':'forbidden'}),'UNKNOWN_TOOL')
    assert backend.effects==1
    r=must(update_role(b['owner'],r,connector_rules=[{'binding_id':binding['id'],'tools':['echo','run']}]))
    run=call(b['alice'],g['token'],'kiln__run',{})
    assert (not run.json()['result'].get('isError')) == (mode=='role')
    assert backend.effects==(2 if mode=='role' else 1)
    # Revoke the role assignment: fixed and dynamic external-only connections both stop.
    assignment=must(b['owner'].get('/api/iam/spaces/team/assignments'))['assignments']
    current=next(x for x in assignment if x['role_id']==r['id'] and x['user_id']=='alice')
    must(b['owner'].put('/api/iam/spaces/team/assignments/'+r['id']+'/alice',json={'active':False,'may_delegate':True,'expected_version':current['version']}))
    assert call(b['alice'],g['token'],'kiln__echo',{'value':'denied'}).status_code in (200,401,403)
    assert backend.effects==(2 if mode=='role' else 1)


@pytest.mark.parametrize('mode',['fixed','role'])
def test_tool_republication_never_silently_changes_fixed_consent(gw,mode):
    app,b,backend=gw;c=connector(b);a=account(b,c);binding=publish(b,a)
    r=role(b['owner'],label='reviewer',project_rules=[],connector_rules=[{'binding_id':binding['id'],'tools':['echo']}])
    assign(b['owner'],r,'alice');g=must(connection(b['alice'],r,mode,confirm_external_mcp=True),201)
    definitions=app.state.gateway.tools(principal(app,g['token']))
    exported=next(t for t in definitions if t['name']=='kiln__echo')
    assert exported['securitySchemes'][0]['scopes']==(['codepier.role_access'] if mode=='role' else ['read'])
    backend.tools[0]['description']='New reviewed definition'
    discovery=must(b['alice'].post('/api/mcp-gateway/accounts/'+a['id']+'/discover'))
    must(b['alice'].post('/api/mcp-gateway/bindings/'+binding['id']+'/publish',json={
        'catalog_hash':discovery['catalog_hash'],'tools':['echo','run'],'confirmed':True,'expected_version':binding['version']}))
    response=call(b['alice'],g['token'],'kiln__echo',{'value':'after review'})
    assert not response.json()['result'].get('isError') if mode=='role' else response.json()['result'].get('isError')
    assert backend.effects==(1 if mode=='role' else 0)
