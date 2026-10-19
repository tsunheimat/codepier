"""Real MCP router contract for the isolated Authentik acceptance client.

No external provider/credentials here; the separate script starts its own real
loopback Authentik instance, performs browser consent and verifies offboarding.
"""
import json

import httpx
import pytest

from scripts.check_oidc_authentik import (
    MCP_ACCEPTANCE_VERSION, initialize_mcp, mcp_request, required, verify_bootstrap_workspace,
)
from tests.identity_fixtures import (team, data, set_member, make_profile,
    make_connection, update_profile, project_policy)  # noqa: F401
from tests.current_model import CurrentModel


def credentials(b, *, live=False):
    set_member(b['owner'],'alice',project_policy('read','write','execute'))
    profile=make_profile(b['owner'],project_policy('read'))
    return profile,make_connection(b['alice'],profile,live=live)


def test_bootstrap_has_explicit_workspace_urls_without_header_state(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from hub.app import create_app
    monkeypatch.setenv('HUB_PUBLIC_URL','http://testserver');monkeypatch.setenv('MCP_PUBLIC_URL','')
    app=create_app(str(tmp_path/'hub'))
    model=CurrentModel(app.state.store);model.user('owner',username='admin')
    personal=app.state.store.one("SELECT id FROM spaces WHERE owner_user_id='owner' AND kind='personal'")['id']
    with app.state.store.transaction():session=app.state.auth.new_session('owner')
    with TestClient(app) as client:
        client.cookies.set('rd_session',session['cookie']);client.headers['X-RD-CSRF']=session['csrf']
        body={'label':'Acceptance team','idempotency_key':'fixture-bootstrap-space'}
        assert client.post('/api/iam/spaces',json=body).status_code==404
        assert client.post('/api/spaces',json=body,headers={'X-CodePier-Space':'removed'}).status_code==400
        before=dict(client.headers)
        assert verify_bootstrap_workspace(client)==personal
        assert dict(client.headers)==before
        space=required(client.post('/api/spaces',json=body),201)
        assert space['kind']=='workspace' and space['id']!=personal
        assert not app.state.store.one("SELECT 1 FROM spaces WHERE kind='legacy'")


def test_acceptance_client_sends_required_headers_and_initialization():
    requests=[]
    def handle(request):
        requests.append(request);body=json.loads(request.content)
        if body['method']=='notifications/initialized':
            assert 'id' not in body
            return httpx.Response(202)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],
            'result':{'protocolVersion':MCP_ACCEPTANCE_VERSION}})
    with httpx.Client(base_url='http://fixture.test',transport=httpx.MockTransport(handle)) as client:
        initialize_mcp(client,'fixture-token')
        mcp_request(client,'fixture-token','tools/call',{'name':'get_profile','arguments':{}})
    assert len(requests)==3
    for request in requests:
        assert request.method=='POST' and request.url.path=='/mcp'
        assert request.headers['authorization']=='Bearer fixture-token'
        assert set(request.headers['accept'].split(', '))=={'application/json','text/event-stream'}
        assert request.headers['mcp-protocol-version']==MCP_ACCEPTANCE_VERSION
        assert request.headers['content-type']=='application/json'
        assert 'cookie' not in request.headers and 'x-codepier-space' not in request.headers
    assert json.loads(requests[0].content)['id']!=json.loads(requests[2].content)['id']


@pytest.mark.parametrize('status',[400,401,403,406,500])
def test_acceptance_client_never_retries_masks_or_echoes_failure_bodies(status):
    requests=[]
    def handle(request):
        requests.append(request);return httpx.Response(status,json={'secret':'do-not-echo-provider-data'})
    with httpx.Client(base_url='http://fixture.test',transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssertionError,match=f'expected 200, got {status}') as caught:
            initialize_mcp(client,'private-fixture-token')
    assert len(requests)==1
    assert 'private-fixture-token' not in str(caught.value)
    assert 'do-not-echo-provider-data' not in str(caught.value)


def test_helper_roundtrips_real_mcp_with_no_panel_credentials(team):
    app,b=team;profile,created=credentials(b)
    client=b['alice'].client;assert not client.cookies
    initialize_mcp(client,created['token'])
    identity=data(mcp_request(client,created['token'],'tools/call',{'name':'get_profile','arguments':{}}))
    assert identity['id']==created['connection_id'] and identity['id']!=profile['id']
    catalog=required(mcp_request(client,created['token'],'tools/list'))['result']['tools']
    assert any(tool['name']=='get_access_context' for tool in catalog)
    assert not client.cookies


@pytest.mark.parametrize('accept',['*/*','application/json','text/event-stream'])
def test_server_rejects_incomplete_media_types(team,accept):
    app,b=team;_,created=credentials(b)
    response=b['alice'].client.post('/mcp',headers={'Authorization':'Bearer '+created['token'],
        'Accept':accept,'MCP-Protocol-Version':MCP_ACCEPTANCE_VERSION},
        json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
    assert response.status_code==406 and response.json()['error']['code']==-32600


def test_transport_preserves_explicit_live_profile_expansion_and_revocation(team):
    app,b=team;profile,grant=credentials(b,live=True)
    client,token=b['alice'].client,grant['token'];initialize_mcp(client,token)
    CurrentModel(app.state.store).project('fixture-new',space='team',folder='folder-team',owner='owner',subpath='new')
    policy={'project_rules':[{'actions':['read','write','execute'],'all_projects':True}]}
    set_member(b['owner'],'alice',policy);update_profile(b['owner'],profile,policy)
    context=data(mcp_request(client,token,'tools/call',{'name':'get_access_context','arguments':{}}))
    assert {p['id'] for p in context['projects']}=={'project-team','fixture-new'}
    assert set(context['scopes'])=={'read','write','execute'}
    assert context['profile']['id']==profile['id'] and context['connection']['id']==grant['connection_id']
    app.state.store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
    response=mcp_request(client,token,'tools/call',{'name':'project_query','arguments':{'operation':'list'}})
    assert response.status_code==403
    assert app.state.store.one('SELECT id FROM grants WHERE id=?',(grant['connection_id'],))['id']==grant['connection_id']


def test_mcp_credential_is_not_overridden_by_another_panel_account(team):
    app,b=team;profile,created=credentials(b)
    identity=data(mcp_request(b['bob'],created['token'],'tools/call',{'name':'get_profile','arguments':{}}))
    assert identity['id']==created['connection_id'] and identity['id']!=profile['id']
    assert mcp_request(b['bob'],'invalid-token','tools/list').status_code==401


@pytest.mark.parametrize("live",[False,True])
def test_admin_publication_follows_live_consent_without_wildcard_delegation(team,live):
    import uuid
    app,b=team
    policy={"project_rules":[{"actions":["read"],"all_projects":True}]}
    set_member(b["owner"],"alice",{**policy,"manage":["profiles"]})
    before=app.state.store.all("SELECT id FROM access_profiles")
    denied=b["alice"].post("/api/spaces/team/profiles",json={"label":"Forbidden wildcard",
        "policy":policy,"idempotency_key":uuid.uuid4().hex})
    assert denied.status_code==403 and denied.json()["error"]["code"]=="DELEGATION_EXCEEDED"
    assert app.state.store.all("SELECT id FROM access_profiles")==before
    profile=make_profile(b["alice"],{})
    connection=make_connection(b["alice"],profile,live=live)
    token=connection["token"]
    def current():
        return data(mcp_request(b["alice"].client,token,"tools/call",{
            "name":"get_access_context","arguments":{}}))
    initial=current()
    assert initial["projects"]==[]
    updated=update_profile(b["owner"],profile,policy)
    assert updated["id"]==profile["id"] and updated["version"]==profile["version"]+1
    CurrentModel(app.state.store).project("future-reviewed",space="team",folder="folder-team",
        owner="owner",subpath="future-reviewed")
    active=current()
    assert {p["id"] for p in active["projects"]}==({"project-team","future-reviewed"} if live else set())
    assert active["connection"]==initial["connection"]
    assert active["profile"]["id"]==profile["id"]
    app.state.store.execute("UPDATE memberships SET active=0 WHERE user_id=? AND space_id=?",("alice","team"))
    rejected=mcp_request(b["alice"].client,token,"tools/call",{"name":"get_access_context","arguments":{}})
    assert rejected.status_code==403
