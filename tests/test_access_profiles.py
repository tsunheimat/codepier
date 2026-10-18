"""Workspace Profiles cap immutable Connections, never share their identity.

Global profile/scopes APIs and old-schema migration tests are retired. Every
positive request uses the actual current member/Profile/Connection contract.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from hub import iam
from shared.crypto import digest
from shared.util import DevError
from tests.identity_fixtures import (team, must, call, data, set_member, make_profile,
    update_profile, make_connection, project_policy, pending_oauth, select_oauth_space)  # noqa: F401


def setup_access(b, *, live=False, actions=('read','write','execute')):
    policy=project_policy(*actions)
    set_member(b['owner'],'alice',policy)
    profile=make_profile(b['owner'],policy)
    return profile,make_connection(b['alice'],profile,live=live)


def principal(app, token):
    return app.state.auth.bearer(Request({'type':'http','method':'POST','path':'/mcp','query_string':b'',
        'headers':[(b'authorization',('Bearer '+token).encode())]}))


def authorize_code(browser, profile, *, live=False):
    pending=pending_oauth(browser);selected=select_oauth_space(browser,pending)
    response=must(browser.post('/api/oauth/requests/'+pending['id']+'/decide',json={
        'allow':True,'space_id':'team','profile_id':profile['id'],'profile_version':profile['version'],
        'selection_version':selected['selection_version'],'consent_mode':'live' if live else 'fixed',
        'confirm_live_profile':live}))
    return {**pending,'code':parse_qs(urlparse(response['redirect']).query)['code'][0]}


def exchange(browser, pending):
    return browser.post('/oauth/token',data={'grant_type':'authorization_code','code':pending['code'],
        'client_id':pending['client_id'],'redirect_uri':pending['redirect_uri'],
        'resource':pending['resource'],'code_verifier':pending['verifier']})


def refresh(browser, pending, tokens):
    return browser.post('/oauth/token',data={'grant_type':'refresh_token',
        'client_id':pending['client_id'],'resource':pending['resource'],'refresh_token':tokens['refresh_token']})


def test_discoverable_identity_is_the_connection_not_the_shared_profile(team):
    app,b=team;profile,created=setup_access(b)
    identity=data(call(b['alice'],created['token'],'get_profile'))
    assert identity['id']==created['connection_id'] and identity['id']!=profile['id']
    assert identity['name']=='CodePier'
    context=data(call(b['alice'],created['token']))
    assert context['connection']['id']==identity['id'] and context['profile']['id']==profile['id']
    assert context['isolation']=='connection'
    assert not {'space_id','role','role_id','current_space_id'} & context.keys()
    second=make_connection(b['alice'],profile)
    assert data(call(b['alice'],second['token'],'get_profile'))['id']!=identity['id']
    assert principal(app,second['token']).profile_id==principal(app,created['token']).profile_id


def test_create_is_idempotent_but_changed_input_never_reuses_identity(team):
    app,b=team
    body={'label':'Unique profile','policy':project_policy('read'),'idempotency_key':uuid.uuid4().hex}
    first=must(b['owner'].post('/api/spaces/team/profiles',json=body))
    assert must(b['owner'].post('/api/spaces/team/profiles',json=body))['id']==first['id']
    changed=b['owner'].post('/api/spaces/team/profiles',json={**body,'label':'Changed'})
    assert changed.status_code==409 and changed.json()['error']['code']=='IDEMPOTENCY_CONFLICT'
    assert len(app.state.store.all('SELECT id FROM access_profiles'))==1


def test_refresh_keeps_original_connection_reconnect_makes_another_identity(team):
    app,b=team;profile,pat=setup_access(b)
    pending=authorize_code(b['alice'],profile);tokens=must(exchange(b['alice'],pending))
    before=data(call(b['alice'],tokens['access_token']))
    renewed=must(refresh(b['alice'],pending,tokens))
    assert data(call(b['alice'],renewed['access_token']))['connection']==before['connection']
    assert before['connection']['id']!=pat['connection_id']
    another=authorize_code(b['alice'],profile);new=must(exchange(b['alice'],another))
    context=data(call(b['alice'],new['access_token']))
    assert context['connection']['id']!=before['connection']['id']
    assert context['profile']['id']==before['profile']['id']==profile['id']
    assert renewed['scope']==new['scope']=='codepier.workspace'
    assert app.state.store.one('SELECT grant_id FROM tokens WHERE hash=?',(digest(renewed['access_token']),))['grant_id']==before['connection']['id']


@pytest.mark.parametrize('live',[False,True])
def test_member_profile_and_consent_intersect_without_flattening_resources(team, live):
    app,b=team
    set_member(b['owner'],'alice',project_policy('read','write','execute'))
    profile=make_profile(b['owner'],project_policy('read'))
    created=make_connection(b['alice'],profile,live=live)
    original=app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))
    update_profile(b['owner'],profile,project_policy('read','write','execute'))
    assert ('write' in data(call(b['alice'],created['token']))['scopes']) is live
    assert app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))==original
    set_member(b['owner'],'alice',project_policy('read'))
    assert data(call(b['alice'],created['token']))['scopes']==['read']
    assert principal(app,created['token']).space_id=='team'


@pytest.mark.parametrize('stage',['before_exchange','after_exchange'])
def test_paused_profile_retains_identity_without_executable_authority(team, stage):
    app,b=team;profile,_=setup_access(b)
    pending=authorize_code(b['alice'],profile,live=True)
    if stage=='after_exchange':tokens=must(exchange(b['alice'],pending))
    update_profile(b['owner'],profile,profile['policy'],enabled=False)
    if stage=='before_exchange':tokens=must(exchange(b['alice'],pending))
    context=data(call(b['alice'],tokens['access_token']))
    # Identity introspection retains read, but there is no read authority over
    # any resource and no executable capability after a Profile pause.
    assert context['projects']==[] and context['scopes']==['read']
    result=call(b['alice'],tokens['access_token'],'exec',{'project':'project-team','command':'true',
        'yield_seconds':0,'idempotency_key':'paused-profile-no-operation'}).json()['result']
    assert result['isError']
    renewed=must(refresh(b['alice'],pending,tokens))
    assert data(call(b['alice'],renewed['access_token']))['connection']==context['connection']
    assert not app.state.store.all('SELECT id FROM operations')


@pytest.mark.parametrize('change',['membership','connection','epoch'])
def test_revocation_between_consent_and_exchange_mints_no_tokens(team, change):
    app,b=team;profile,_=setup_access(b);pending=authorize_code(b['alice'],profile)
    row=app.state.store.one('SELECT grant_id FROM oauth_codes WHERE hash=?',(digest(pending['code']),))
    if change=='membership':app.state.store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
    elif change=='connection':app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?',(row['grant_id'],))
    else:app.state.store.execute("UPDATE iam_users SET epoch=epoch+1 WHERE user_id='alice'")
    response=exchange(b['alice'],pending)
    assert response.status_code in {400,403},response.text
    assert not app.state.store.all('SELECT id FROM tokens WHERE grant_id=?',(row['grant_id'],))


def test_stale_profile_consent_and_cross_workspace_selection_are_atomic(team):
    app,b=team;profile,_=setup_access(b)
    pending=pending_oauth(b['alice']);selected=select_oauth_space(b['alice'],pending)
    updated=update_profile(b['owner'],profile,project_policy('read'))
    body={'allow':True,'space_id':'team','profile_id':profile['id'],'profile_version':profile['version'],
          'selection_version':selected['selection_version']}
    assert b['alice'].post('/api/oauth/requests/'+pending['id']+'/decide',json=body).status_code==409
    other=make_profile(b['owner'],{},space='other-team')
    response=b['alice'].post('/api/oauth/requests/'+pending['id']+'/decide',json={**body,
        'profile_id':other['id'],'profile_version':other['version']})
    assert response.status_code==404
    assert not app.state.store.all('SELECT * FROM oauth_codes')
    assert app.state.store.one('SELECT used FROM oauth_requests WHERE id=?',(pending['id'],))['used']==0
    response=b['alice'].post('/api/oauth/requests/'+pending['id']+'/decide',json={**body,'profile_version':updated['version']})
    assert response.status_code==200,response.text


def test_profile_shrink_rechecks_queue_without_replacing_original_work(team):
    app,b=team;profile,created=setup_access(b,live=True)
    values={'project':'project-team','command':'printf private','yield_seconds':0,
        'idempotency_key':'queued-before-profile-shrink'}
    receipt=data(call(b['alice'],created['token'],'exec',values))
    row=app.state.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    assert app.state.runtime.permission_error(row) is None
    update_profile(b['owner'],profile,project_policy('read'))
    assert app.state.runtime.permission_error(row)
    assert app.state.store.one('SELECT state,attempts FROM operations WHERE id=?',(row['id'],))=={'state':'queued','attempts':0}
    assert call(b['alice'],created['token'],'exec',values).json()['result']['isError']
    assert len(app.state.store.all('SELECT id FROM operations'))==1


def test_stale_edit_aba_and_reenable_never_revive_revoked_connection(team):
    app,b=team;profile,created=setup_access(b)
    updated=update_profile(b['owner'],profile,project_policy('read'))
    updated=update_profile(b['owner'],updated,profile['policy'])
    stale=b['owner'].put('/api/spaces/team/profiles/'+profile['id'],json={'label':profile['label'],
        'policy':profile['policy'],'expected_version':profile['version'],'idempotency_key':uuid.uuid4().hex})
    assert stale.status_code==409
    must(b['alice'].post('/api/spaces/team/connections/'+created['connection_id']+'/revoke'))
    updated=update_profile(b['owner'],updated,profile['policy'],enabled=False)
    update_profile(b['owner'],updated,profile['policy'],enabled=True)
    assert call(b['alice'],created['token']).status_code==401
    assert app.state.store.one('SELECT revoked FROM grants WHERE id=?',(created['connection_id'],))['revoked']==1


@pytest.mark.parametrize('field,value',[
    ('label',''),('label','\x00invalid'),('label','x'*81),('policy',[]),('policy',{'project_rules':'invalid'}),
    ('policy',{'project_rules':[{'projects':['project-team'],'actions':['owner']}]}),
    ('policy',{'manage':['workspace']}),('policy',{'project_rules':[{'projects':['project-other-team'],'actions':['read']}]}),
])
def test_profile_invalid_input_never_creates_partial_configuration(team, field, value):
    app,b=team
    response=b['owner'].post('/api/spaces/team/profiles',json={
        'label':'Invalid input','policy':project_policy('read'),'idempotency_key':uuid.uuid4().hex,field:value})
    assert response.status_code in {400,403,404,422},response.text
    assert not app.state.store.all('SELECT id FROM access_profiles')


@pytest.mark.parametrize('header,value',[('X-RD-CSRF','wrong'),('Origin','https://other.invalid')])
def test_profile_management_requires_session_csrf_and_same_origin(team, header, value):
    app,b=team
    response=b['owner'].post('/api/spaces/team/profiles',json={'label':'Not created','policy':{},
        'idempotency_key':uuid.uuid4().hex},headers={header:value})
    assert response.status_code==403
    assert not app.state.store.all('SELECT id FROM access_profiles')
    assert b['bob'].post('/api/spaces/team/profiles',json={'label':'Not delegated','policy':{},
        'idempotency_key':uuid.uuid4().hex}).status_code==403


@pytest.mark.parametrize('malformed',['null','[]','42','"read"','{"project_rules":"all"}',
    '{"project_rules":[{"all_projects":true,"actions":["root"]}]}'])
def test_malformed_stored_policy_fails_closed_without_token_fallback(team, malformed):
    app,b=team;profile,created=setup_access(b)
    app.state.store.execute('UPDATE access_profiles SET policy=? WHERE id=?',(malformed,profile['id']))
    response=call(b['alice'],created['token'])
    assert response.status_code in {400,401,403},response.text
    assert not app.state.store.all('SELECT id FROM operations')
    assert app.state.store.one('SELECT profile_id FROM grants WHERE id=?',(created['connection_id'],))['profile_id']==profile['id']


@pytest.mark.parametrize('column,value',[('space_id','other-team'),('profile_id','another'),('user_id','bob')])
def test_connection_binding_cannot_be_reassigned_even_by_database_mutation(team, column, value):
    app,b=team;_,created=setup_access(b)
    before=app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))
    with pytest.raises(sqlite3.IntegrityError,match='connection identity is immutable'):
        app.state.store.execute(f'UPDATE grants SET {column}=? WHERE id=?',(value,created['connection_id']))
    assert app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))==before


@pytest.mark.parametrize('tool',['computer_observe','browser_snapshot','browser_action'])
def test_narrowing_computer_permission_blocks_prior_private_media(team, tool):
    app,b=team;profile,created=setup_access(b,live=True,actions=('read','computer'))
    now=time.time();identifier=uuid.uuid4().hex
    app.state.store.execute('''INSERT INTO operations
        (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id,grant_id)
        VALUES(?,'device-team','project-team',?,?,'{}','','succeeded',?,?,'team','alice',?)''',
        (identifier,'mcp:'+created['connection_id']+':fixture',tool,now,now,created['connection_id']))
    assert app.state.runtime.operation_row(identifier,principal(app,created['token']))['id']==identifier
    update_profile(b['owner'],profile,project_policy('read'))
    with pytest.raises(DevError): app.state.runtime.operation_row(identifier,principal(app,created['token']))
    assert iam.live_principal(app.state.store,principal(app,created['token'])).scopes=={'read'}
