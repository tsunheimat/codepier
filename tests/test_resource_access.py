"""Real action/resource pairs across workspace Projects, whole VPS and MCP tools."""
import json
import time
import uuid

import pytest

from hub.permissions import project_scopes
from tests.identity_fixtures import (team, must, call, data, set_member, make_profile,
    make_connection, update_profile, project_policy)  # noqa: F401
from tests.test_access_profiles import principal
from tests.test_mcp_gateway import (gw, connector, account, publish, authorize,
    update_policy, error_code)  # noqa: F401


def inventory(app, browser, *, space='team', device='device-team'):
    rows=[]
    for name in ('Testing','Production'):
        resource=must(browser.post('/api/vps',json={'name':name,'host':name.lower()+'.invalid',
            'idempotency_key':uuid.uuid4().hex}))
        rows.append(must(browser.post('/api/spaces/'+space+'/vps',json={
            'resource_id':resource['id'],'name':name,'runner_device_id':device,'connection_ref':name.lower(),
            'display_mode':'top_level','projects':[],'idempotency_key':uuid.uuid4().hex})))
    return rows


def another_project(app, *, identifier='future', space='team', folder='folder-team'):
    app.state.store.execute('''INSERT INTO projects
        (id,alias,alias_key,folder_id,subpath,space_id,owner_user_id,created)
        VALUES(?,?,?,?,?,?,'owner',?)''',(identifier,identifier,identifier,folder,identifier,space,time.time()))
    return identifier


def access(b, policy, *, live=False):
    set_member(b['owner'],'alice',policy)
    profile=make_profile(b['owner'],policy)
    return profile,make_connection(b['alice'],profile,live=live)


def result_error(response, code):
    value=response.json()['result'];assert value.get('isError'),value
    result=value.get('structuredContent') or json.loads(value['content'][0]['text'])
    assert result['error']['code']==code,result


def execute(browser, token, vps, *, project=None, key=None):
    args={'target':'vps:'+vps['id'],'command':'printf isolated-test','yield_seconds':0,
        'idempotency_key':key or uuid.uuid4().hex}
    if project is not None:args['project']=project
    return call(browser,token,'exec',args)


@pytest.mark.parametrize('live',[False,True])
def test_worker_action_pairs_authorize_testing_not_production_or_other_project(team, live):
    app,b=team;testing,production=inventory(app,b['owner']);another_project(app)
    policy={'project_rules':[{'actions':['read','write','execute'],'projects':['project-team']},
        {'actions':['read'],'projects':['future']}],
        'vps_rules':[{'actions':['read','execute'],'vps':[testing['id']]},
                     {'actions':['read'],'vps':[production['id']]}]}
    profile,created=access(b,policy,live=live)
    p=principal(app,created['token'])
    assert project_scopes(app.state.store,p,'future')=={'read'}
    receipt=data(execute(b['alice'],created['token'],testing))
    result_error(execute(b['alice'],created['token'],production),'VPS_NOT_FOUND')
    result_error(call(b['alice'],created['token'],'exec',{'project':'future','command':'true',
        'yield_seconds':0,'idempotency_key':uuid.uuid4().hex}),'WORKSPACE_POLICY_DENIED')
    records=must(b['alice'].get('/api/spaces/team/connections'))['connections']
    record=next(row for row in records if row['id']==created['connection_id'])
    assert record['profile']['id']==profile['id']
    assert {(tuple(row['vps']),tuple(sorted(row['actions'])))
        for row in record['permissions']['vps_rules']}=={
            ((testing['id'],),('execute','read')),((production['id'],),('read',))}
    assert '"password"' not in json.dumps(record) and '"private_key"' not in json.dumps(record)
    operation=app.state.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    request=json.loads(app.state.store.decrypt(operation['payload']))
    assert operation['project_id'] is None
    assert app.state.runtime.permission_error(operation,request) is None
    update_profile(b['owner'],profile,{**policy,'vps_rules':[{'actions':['read'],'vps':[testing['id'],production['id']]}]})
    assert app.state.runtime.permission_error(operation,request)
    result_error(execute(b['alice'],created['token'],testing),'VPS_NOT_FOUND')
    assert b['owner'].get('/api/operations/'+receipt['operation_id']).status_code==403


@pytest.mark.parametrize('live',[False,True])
def test_fixed_snapshot_and_explicit_live_profile_future_consent(team, live):
    app,b=team;testing,production=inventory(app,b['owner'])
    policy={**project_policy('read'),'vps_rules':[{'actions':['read'],'vps':[testing['id']]}]}
    profile,created=access(b,policy,live=live)
    before=app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))
    another_project(app)
    expanded={'project_rules':[{'actions':['read','write','execute'],'all_projects':True}],
        'vps_rules':[{'actions':['read','execute'],'vps':[testing['id'],production['id']]}]}
    set_member(b['owner'],'alice',expanded)
    profile=update_profile(b['owner'],profile,expanded)
    context=data(call(b['alice'],created['token']))
    assert {p['id'] for p in context['projects']}==({'project-team','future'} if live else {'project-team'})
    assert project_scopes(app.state.store,principal(app,created['token']),'project-team')==(
        {'read','write','execute'} if live else {'read'})
    assert {v['id'] for v in data(call(b['alice'],created['token'],'vps'))['vps']}==(
        {testing['id'],production['id']} if live else {testing['id']})
    assert app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))==before
    update_profile(b['owner'],profile,expanded,enabled=False)
    assert data(call(b['alice'],created['token'],'vps'))['vps']==[]
    record=next(x for x in must(b['alice'].get('/api/spaces/team/connections'))['connections'] if x['id']==created['connection_id'])
    assert record['status']=='disabled' and not record['permissions']['project_rules']


def test_connection_confirmation_version_and_request_key_are_transactional(team):
    app,b=team;policy=project_policy('read');set_member(b['owner'],'alice',policy)
    profile=make_profile(b['owner'],policy)
    body={'name':'Connection','profile_id':profile['id'],'profile_version':profile['version'],
        'consent_mode':'live','confirm_live_profile':False,'idempotency_key':uuid.uuid4().hex}
    response=b['alice'].post('/api/spaces/team/connections',json=body)
    assert response.status_code==400 and response.json()['error']['code']=='LIVE_CONSENT_REQUIRED'
    assert not app.state.store.all('SELECT id FROM grants')
    assert b['alice'].post('/api/spaces/team/connections',json={**body,'confirm_live_profile':True,
        'profile_version':profile['version']+1}).status_code==409
    body.update(consent_mode='fixed')
    first=must(b['alice'].post('/api/spaces/team/connections',json=body),201)
    replay=must(b['alice'].post('/api/spaces/team/connections',json=body),201)
    assert replay['connection_id']==first['connection_id'] and replay['replayed'] and 'token' not in replay
    assert b['alice'].post('/api/spaces/team/connections',json={**body,'name':'Changed'}).status_code==409


@pytest.mark.parametrize('end',['expiry','revoke'])
def test_expired_and_revoked_connections_reject_real_calls(team, end):
    app,b=team;_,created=access(b,project_policy('read'))
    assert data(call(b['alice'],created['token']))['projects']
    if end=='expiry':app.state.store.execute('UPDATE tokens SET expires=? WHERE grant_id=?',(time.time()-1,created['connection_id']))
    else:must(b['alice'].post('/api/spaces/team/connections/'+created['connection_id']+'/revoke'))
    assert call(b['alice'],created['token']).status_code==401


def test_vps_transport_is_explicit_without_a_project_execution_route(team):
    app,b=team;testing,_=inventory(app,b['owner'])
    resource=must(b['owner'].post('/api/vps',json={'name':'Inventory only','host':'metadata.invalid',
        'idempotency_key':uuid.uuid4().hex}))
    invalid=b['owner'].post('/api/spaces/team/vps',json={'resource_id':resource['id'],
        'name':'No transport','display_mode':'top_level','idempotency_key':uuid.uuid4().hex})
    assert invalid.status_code==422
    _,created=access(b,{'vps_rules':[{'vps':[testing['id']],'actions':['read','execute']}]})
    receipt=data(execute(b['alice'],created['token'],testing))
    assert app.state.store.one('SELECT project_id FROM operations WHERE id=?',(receipt['operation_id'],))['project_id'] is None
    result_error(execute(b['alice'],created['token'],{'id':resource['id']}),'VPS_NOT_FOUND')


def test_resource_use_never_grants_inventory_or_authorization_administration(team):
    app,b=team;_,created=access(b,project_policy('read','write','execute'))
    owned=must(b['owner'].post('/api/vps',json={'name':'Owner inventory','host':'metadata.invalid',
        'idempotency_key':uuid.uuid4().hex}))
    denied=b['alice'].put('/api/vps/'+owned['id'],json={'name':'Not Alice owned','host':'metadata.invalid',
        'expected_version':owned['version'],'idempotency_key':uuid.uuid4().hex})
    assert denied.status_code==404 and denied.json()['error']['code']=='INVENTORY_RESOURCE_NOT_FOUND'
    own=must(b['alice'].post('/api/vps',json={'name':'Alice inventory','host':'metadata.invalid',
        'idempotency_key':uuid.uuid4().hex}))
    # Registering one's own inventory is not a workspace resource grant.
    assert b['alice'].post('/api/spaces/team/vps',json={'resource_id':own['id'],'name':'No grant',
        'runner_device_id':'device-team','connection_ref':'testing','display_mode':'top_level',
        'idempotency_key':uuid.uuid4().hex}).status_code==403
    assert not app.state.store.all('SELECT id FROM space_vps')
    assert b['alice'].post('/api/spaces/team/profiles',json={'label':'Admin','policy':{},
        'idempotency_key':uuid.uuid4().hex}).status_code==403
    assert b['alice'].get('/api/spaces/team/connections').status_code==200
    assert created['connection_id'] not in b['bob'].get('/api/spaces/team/connections').text
    assert b['alice'].get('/api/resources/project/project-other-team').status_code in {403,404}
    assert principal(app,created['token']).admin is False


@pytest.mark.parametrize('live',[False,True])
def test_external_binding_accounts_and_profile_edits_are_real_effect_checks(gw, live):
    app,b,backend=gw;c=connector(b);a=account(b,c);binding=publish(b,a)
    other=account(b,c,token='UPSTREAM_TOKEN_B');other_binding=publish(b,other,alias='other')
    _,profile,created=authorize(b,binding,tools=['echo'],live=live)
    assert not call(b['alice'],created['token'],'kiln__echo',{'value':'permitted'}).json()['result'].get('isError')
    assert backend.effects==1
    assert error_code(call(b['alice'],created['token'],'kiln__run',{}))=='UNKNOWN_TOOL'
    assert error_code(call(b['alice'],created['token'],'other__echo',{'value':'forbidden'}))=='UNKNOWN_TOOL'
    assert backend.effects==1 and other_binding['id']!=binding['id']
    update_policy(b,profile,[{'binding_id':binding['id'],'tools':['echo','run']}])
    response=call(b['alice'],created['token'],'kiln__run',{})
    assert (not response.json()['result'].get('isError')) is live
    assert backend.effects==(2 if live else 1)
    set_member(b['owner'],'alice',{})
    denied=call(b['alice'],created['token'],'kiln__echo',{'value':'denied'})
    assert denied.json()['result']['isError']
    assert backend.effects==(2 if live else 1)


@pytest.mark.parametrize('live',[False,True])
def test_tool_republication_never_changes_fixed_consent_silently(gw, live):
    app,b,backend=gw;c=connector(b);a=account(b,c);binding=publish(b,a)
    _,_,created=authorize(b,binding,tools=['echo'],live=live)
    definition=next(t for t in app.state.gateway.tools(principal(app,created['token'])) if t['name']=='kiln__echo')
    assert definition['securitySchemes'][0]['scopes']==['codepier.workspace']
    backend.tools[0]['description']='New reviewed definition'
    discovered=must(b['owner'].post('/api/spaces/team/mcp-gateway/accounts/'+a['id']+'/discover'))
    must(b['owner'].post('/api/spaces/team/mcp-gateway/bindings/'+binding['id']+'/publish',json={
        'catalog_hash':discovered['catalog_hash'],'tools':['echo','run'],'confirmed':True,'expected_version':binding['version']}))
    response=call(b['alice'],created['token'],'kiln__echo',{'value':'after review'})
    assert (not response.json()['result'].get('isError')) is live
    assert backend.effects==(1 if live else 0)
