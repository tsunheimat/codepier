"""Real HTTP composition: login, explicit workspace resources and OAuth identity."""
from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from tests.workspace_fixtures import WorkspaceFixture


@pytest.fixture
def web(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_BUILTIN_AGENT_ENABLED','false')
    monkeypatch.setenv('HUB_PUBLIC_URL','https://workspace.test')
    monkeypatch.delenv('MCP_PUBLIC_URL',raising=False)
    monkeypatch.setenv('CODEPIER_MCP_GATEWAY','1')
    monkeypatch.setenv('OAUTH_REDIRECT_HOSTS','client.example')
    root=tmp_path/'hub'
    seed=WorkspaceFixture(root)
    seed.close()
    app=create_app(str(root))
    with TestClient(app,base_url='https://workspace.test') as client:
        response=client.post('/api/login',json={'username':'alice','password':'a-valid-test-password'},headers={'Origin':'https://workspace.test'})
        assert response.status_code==200,response.text
        client.headers.update({'X-RD-CSRF':response.json()['csrf'],'Origin':'https://workspace.test'})
        yield client,app


def profile(client,space='alice',name='Developer'):
    response=client.post(f'/api/spaces/{space}/profiles',json={
        'label':name,'policy':{'project_rules':[{'projects':['project'],'actions':['read','write','execute']}]},
        'idempotency_key':'create-profile-'+space+'-'+name})
    assert response.status_code==200,response.text
    return response.json()


def connection(client,profile_id,version=1,**kw):
    response=client.post('/api/spaces/alice/connections',json={'name':'Test connection','profile_id':profile_id,
        'profile_version':version,'idempotency_key':'connection-key-001',**kw})
    assert response.status_code==201,response.text
    return response.json()


def test_startup_and_explicit_workspace_pages(web):
    client,_=web
    me=client.get('/api/iam/me')
    assert me.status_code==200,me.text
    assert 'current_space_id' not in me.json() and 'space_id' not in me.json()
    assert client.get('/api/spaces/alice').status_code==200
    assert client.get('/api/spaces/bob').status_code==403
    assert client.get('/api/projects').status_code==200
    assert client.get('/api/projects/project').status_code==200
    assert client.get('/api/settings').status_code==200
    assert client.get('/api/spaces/alice/projects').status_code==200
    assert client.get('/api/spaces/alice/folders').status_code==200
    assert client.get('/api/spaces/alice/members').status_code==200
    assert client.get('/api/spaces/alice/shell').status_code==200
    assert client.get('/api/spaces/alice/storage').status_code==200
    assert client.get('/api/spaces/alice/vps').status_code==200
    assert client.get('/api/spaces/alice/mcp-gateway').status_code==200
    assert client.get('/api/spaces/alice/external-integrations').status_code==200
    assert client.get('/api/projects/project/shell').status_code==200


def test_legacy_routes_are_not_registered(web):
    _,app=web
    paths={getattr(route,'path','') for route in app.routes}
    assert not paths & {'/api/access-roles','/api/access-profiles','/api/grants','/api/source-roots','/api/project-routing'}
    assert not any('role-assignment' in p or 'current-space' in p for p in paths)


def test_new_connection_mandatory_profile_and_private_identity(web):
    client,app=web
    response=client.post('/api/spaces/alice/connections',json={'name':'No profile','idempotency_key':'no-profile-test'})
    assert response.status_code==422
    saved=profile(client)
    created=connection(client,saved['id'])
    assert created['connection_id']!=saved['id']
    replay=connection(client,saved['id'])
    assert replay['connection_id']==created['connection_id'] and 'token' not in replay
    row=app.state.store.one('SELECT * FROM grants WHERE id=?',(created['connection_id'],))
    assert row['space_id']=='alice' and row['profile_id']==saved['id']
    assert row['consent_mode']=='fixed'
    assert client.get('/api/spaces/alice/connections').json()['connections'][0]['status']=='active'


def test_profile_stale_version_and_cross_workspace_are_rejected(web):
    client,_=web
    saved=profile(client)
    response=client.post('/api/spaces/alice/connections',json={'name':'Stale','profile_id':saved['id'],
        'profile_version':99,'idempotency_key':'connection-stale-profile'})
    assert response.status_code==409,response.text
    assert client.post('/api/spaces/bob/connections',json={'name':'Wrong','profile_id':saved['id'],
        'profile_version':1,'idempotency_key':'connection-cross-space'}).status_code==403


def oauth_pending(client):
    response=client.post('/oauth/register',json={'client_name':'Workspace OAuth test','redirect_uris':['https://client.example/callback'],
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'})
    assert response.status_code==201,response.text
    client_id=response.json()['client_id']
    verifier='a'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response=client.get('/oauth/authorize',params={'client_id':client_id,'redirect_uri':'https://client.example/callback',
        'response_type':'code','scope':'codepier.workspace','resource':'https://workspace.test/mcp',
        'code_challenge_method':'S256','code_challenge':challenge,'state':'original-client-state'},follow_redirects=False)
    assert response.status_code in {302,303,307},response.text
    location=response.headers['location']
    # The redirect points at the local authorization UI; extract only its request ID.
    query=parse_qs(urlparse(location).query)
    request_id=(query.get('request') or query.get('id') or [location.rsplit('=',1)[-1]])[0]
    return request_id,client_id,verifier


def test_oauth_two_steps_and_single_use_code(web):
    client,app=web
    saved=profile(client)
    identifier,client_id,verifier=oauth_pending(client)
    detail=client.get('/api/oauth/requests/'+identifier)
    assert detail.status_code==200,detail.text
    assert detail.json()['selected_space_id'] is None
    version=detail.json()['selection_version']
    premature=client.post('/api/oauth/requests/'+identifier+'/decide',json={
        'allow':True,'space_id':'alice','profile_id':saved['id'],'profile_version':1,'selection_version':version})
    assert premature.status_code==409,premature.text
    choice=client.post('/api/oauth/requests/'+identifier+'/select-space',json={'space_id':'alice','selection_version':version})
    assert choice.status_code==200,choice.text
    assert choice.json()['profiles'][0]['id']==saved['id']
    version=choice.json()['selection_version']
    path='/api/oauth/requests/'+identifier+'/decide'
    consent=client.post(path,json={'allow':True,'space_id':'alice','profile_id':saved['id'],
        'profile_version':1,'selection_version':version})
    assert consent.status_code==200,consent.text
    query=parse_qs(urlparse(consent.json()['redirect']).query)
    assert query['state']==['original-client-state']
    token_body={'grant_type':'authorization_code','client_id':client_id,'redirect_uri':'https://client.example/callback',
        'resource':'https://workspace.test/mcp','code':query['code'][0],'code_verifier':verifier}
    result=client.post('/oauth/token',data=token_body)
    assert result.status_code==200,result.text
    assert result.json()['scope']=='codepier.workspace'
    assert client.post('/oauth/token',data=token_body).status_code==400
    grant=app.state.store.one('SELECT * FROM grants WHERE client_id=?',(client_id,))
    assert grant['space_id']=='alice' and grant['profile_id']==saved['id']
    assert not app.state.store.one("SELECT id FROM tokens WHERE grant_id=? AND kind='pat'",(grant['id'],))
    refreshed=client.post('/oauth/token',data={'grant_type':'refresh_token','client_id':client_id,
        'refresh_token':result.json()['refresh_token'],'resource':'https://workspace.test/mcp'})
    assert refreshed.status_code==200,refreshed.text


def test_csrf_and_stale_consent_selection(web):
    client,_=web
    saved=profile(client)
    identifier,_,_=oauth_pending(client)
    details=client.get('/api/oauth/requests/'+identifier).json()
    choice=client.post('/api/oauth/requests/'+identifier+'/select-space',json={'space_id':'alice','selection_version':details['selection_version']})
    assert choice.status_code==200,choice.text
    stale=client.post('/api/oauth/requests/'+identifier+'/select-space',json={'space_id':'alice','selection_version':details['selection_version']})
    assert stale.status_code==409
    no_csrf=client.post('/api/spaces/alice/profiles',json={'label':'No CSRF','policy':{},'idempotency_key':'no-csrf-key'},headers={'X-RD-CSRF':'wrong'})
    assert no_csrf.status_code==403
