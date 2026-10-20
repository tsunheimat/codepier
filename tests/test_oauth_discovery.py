"""Only workspace Connection OAuth is advertised or accepted after cutoff."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from shared.workspace_contracts import WORKSPACE_SCOPE
from shared.util import DevError
from tests.test_audit_api import api, rpc  # noqa: F401


def authorize(client, scopes):
    registration=client.post('/oauth/register',json={'redirect_uris':['http://localhost:12345/callback']})
    assert registration.status_code==201,registration.text
    challenge=base64.urlsafe_b64encode(hashlib.sha256(b'v'*64).digest()).rstrip(b'=').decode()
    return client.get('/oauth/authorize',params={
        'response_type':'code','client_id':registration.json()['client_id'],
        'redirect_uri':'http://localhost:12345/callback','scope':' '.join(scopes),
        'code_challenge':challenge,'code_challenge_method':'S256','resource':'http://testserver/mcp'},
        follow_redirects=False)


def metadata_response(client, path, **kwargs):
    response=client.get(path,**kwargs)
    assert response.status_code==200,response.text
    assert response.headers['access-control-allow-origin']=='*'
    assert response.headers['cache-control']=='no-store'
    return response.json()


@pytest.mark.parametrize('path',[
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp',
    '/.well-known/oauth-authorization-server',
])
def test_all_advertised_scopes_form_one_valid_workspace_request(api, path):
    app,client,_=api
    metadata=metadata_response(client,path)
    assert metadata['scopes_supported']==[WORKSPACE_SCOPE]
    before=app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    response=authorize(client,metadata['scopes_supported'])
    assert response.status_code==303,response.text
    identifier=parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]
    row=app.state.store.one('SELECT * FROM oauth_requests WHERE id=?',(identifier,))
    assert json.loads(row['scopes'])==[WORKSPACE_SCOPE]
    detail=client.get('/api/oauth/requests/'+identifier)
    assert detail.status_code==200,detail.text
    assert detail.json()['selected_space_id'] is None
    assert not app.state.store.all('SELECT * FROM oauth_codes')
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n']==before


@pytest.mark.parametrize('path',[
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp',
])
@pytest.mark.parametrize('mode',['fixed','role','invalid',''])
def test_removed_authorization_mode_discovery_does_not_redirect_or_fall_back(api, path, mode):
    _,client,_=api
    response=client.get(path,params={'authorization':mode},follow_redirects=False)
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='INVALID_REQUEST'
    assert 'location' not in response.headers


def test_unauthenticated_challenge_advertises_only_canonical_workspace_metadata(api):
    _,client,_=api
    response=client.post('/mcp',json={})
    assert response.status_code==401
    challenge=response.headers['WWW-Authenticate']
    metadata_url=re.search(r'resource_metadata="([^"]+)"',challenge).group(1)
    scopes=re.search(r'scope="([^"]+)"',challenge).group(1).split()
    assert scopes==[WORKSPACE_SCOPE]
    assert not parse_qs(urlsplit(metadata_url).query)
    metadata=metadata_response(client,metadata_url)
    assert metadata['scopes_supported']==[WORKSPACE_SCOPE]
    assert metadata['resource']=='http://testserver/mcp'
    assert authorize(client,metadata['scopes_supported']).status_code==303


@pytest.mark.parametrize('selector,value',[
    ('authorization','fixed'),('authorization','role'),('role','arbitrary'),
    ('space','another-space'),('profile','another-profile'),
])
@pytest.mark.parametrize('authenticated',[False,True])
def test_mcp_entry_has_no_account_or_authorization_selector(api, selector, value, authenticated):
    app,client,token=api
    headers={'Accept':'application/json, text/event-stream'}
    if authenticated: headers['Authorization']='Bearer '+token
    response=client.post('/mcp',params={selector:value},headers=headers,
        json={'jsonrpc':'2.0','id':1,'method':'ping'})
    assert response.status_code==400,response.text
    assert 'result' not in response.json()
    assert not app.state.store.all('SELECT id FROM operations')


def test_read_only_connection_cannot_flatten_workspace_scope_into_execute(api):
    app,client,token=api
    response=rpc(client,token,{'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'exec','arguments':{'project':'project','command':'must-not-run',
            'idempotency_key':'workspace-discovery-no-execution','yield_seconds':0}}})
    assert response.status_code==200,response.text
    result=response.json()['result']
    assert result['isError']
    assert result['structuredContent']['error']['code']=='WORKSPACE_POLICY_DENIED'
    assert not app.state.store.all('SELECT id FROM operations')


def test_step_up_challenge_cannot_reintroduce_role_or_action_oauth_scopes(api, monkeypatch):
    app,client,token=api
    async def insufficient(*args,**kwargs):
        raise DevError('INSUFFICIENT_SCOPE','Additional consent required',403)
    monkeypatch.setattr(app.state.runtime,'invoke',insufficient)
    response=rpc(client,token,{'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'get_access_context','arguments':{}}})
    result=response.json()['result']
    assert result['isError']
    challenge=result['_meta']['mcp/www_authenticate'][0]
    metadata_url=re.search(r'resource_metadata="([^"]+)"',challenge).group(1)
    assert not parse_qs(urlsplit(metadata_url).query)
    assert re.search(r'scope="([^"]+)"',challenge).group(1)==WORKSPACE_SCOPE
    assert metadata_response(client,metadata_url)['scopes_supported']==[WORKSPACE_SCOPE]
    assert not app.state.store.all('SELECT id FROM operations')


@pytest.mark.parametrize('scopes',[
    ['read'],['read','write','execute','computer'],['codepier.role_access'],
    [WORKSPACE_SCOPE,'read'],[WORKSPACE_SCOPE,'codepier.role_access'],
    ['write'],['read','unknown'],[],[WORKSPACE_SCOPE,WORKSPACE_SCOPE],
])
def test_invalid_or_retired_scopes_create_no_authority(api, scopes):
    app,client,_=api
    before={table:app.state.store.one(f'SELECT count(*) AS n FROM {table}')['n']
        for table in ('oauth_requests','oauth_codes','grants','tokens')}
    response=authorize(client,scopes)
    assert response.status_code==400
    assert response.json()['error']['code']=='INVALID_SCOPE'
    assert {table:app.state.store.one(f'SELECT count(*) AS n FROM {table}')['n'] for table in before}==before


def test_invalid_scope_response_is_readable_utf8_without_a_legacy_upgrade_instruction(api):
    _,client,_=api
    response=authorize(client,['read','codepier.role_access'])
    assert response.headers['content-type'].split(';',1)[0]=='application/json'
    assert json.loads(response.content.decode('utf-8'))['error']['message']==(
        'Only codepier.workspace is supported; choose resource permissions at workspace/Profile consent')
    assert '�' not in response.text
