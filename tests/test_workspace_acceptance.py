"""HTTP identity/output contracts and cross-workspace history without ambient state."""
import json
import uuid

import jsonschema
import pytest

from shared.contracts import OUTPUT_SCHEMAS
from tests.test_workspace_http import web, profile, connection  # noqa: F401


def rpc(client, token, name, arguments=None, meta=None):
    params={'name':name,'arguments':arguments or {}}
    if meta: params['_meta']=meta
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,
        'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'},
        json={'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':'tools/call','params':params})
    assert response.status_code==200,response.text
    result=response.json()['result']
    assert not result.get('isError'),result
    return result['structuredContent'] if 'structuredContent' in result else json.loads(result['content'][0]['text'])


def test_connection_identity_and_context_match_advertised_output_schemas(web):
    client,_=web
    p=profile(client);created=connection(client,p['id'])
    identity=rpc(client,created['token'],'get_profile')
    context=rpc(client,created['token'],'get_access_context')
    jsonschema.validate(identity,OUTPUT_SCHEMAS['get_profile'])
    jsonschema.validate(context,OUTPUT_SCHEMAS['get_access_context'])
    assert identity['id']==created['connection_id']!=p['id']
    assert context['connection']==identity and context['profile']['id']==p['id']
    assert context['isolation']=='connection' and 'space_id' not in context


@pytest.mark.parametrize('query',['authorization=role','authorization=fixed','space=alice','role=admin','profile=developer'])
def test_mcp_identity_cannot_be_selected_by_url(web,query):
    client,_=web
    response=client.post('/mcp?'+query,json={})
    assert response.status_code==400
    assert 'connection' in response.text


def test_unauthed_mcp_challenge_has_only_workspace_scope(web):
    client,_=web
    response=client.post('/mcp',json={})
    assert response.status_code==401
    header=response.headers['WWW-Authenticate']
    assert 'scope="codepier.workspace"' in header and 'role_access' not in header
    metadata=client.get('/.well-known/oauth-protected-resource/mcp')
    assert metadata.status_code==200,metadata.text
    assert metadata.json()['scopes_supported']==['codepier.workspace']


def test_owner_can_list_audit_across_pages_without_merging_connection_histories(web):
    client,_=web
    p=profile(client);first=connection(client,p['id'])
    other=client.post('/api/spaces/alice/connections',json={'name':'Second','profile_id':p['id'],
        'profile_version':1,'idempotency_key':'another-connection-0001'}).json()
    for index,created in enumerate([first,other]):
        rpc(client,created['token'],'get_profile',meta={'openai/session':'original-session-'+str(index)})
    # A human may see their connections together, but the MCP connections cannot.
    result=client.get('/api/conversations')
    assert result.status_code==200,result.text
    rows=result.json()['conversations'];assert len(rows)==2
    assert {r['grant_id'] for r in rows}=={first['connection_id'],other['connection_id']}
    for created in [first,other]:
        own=rpc(client,created['token'],'conversations')
        assert len(own['conversations'])==1
        assert own['conversations'][0]['grant_id']==created['connection_id']
    for url,field in [('/api/audit/sessions','sessions'),('/api/call-log','operations')]:
        response=client.get(url);assert response.status_code==200,response.text
        assert len(response.json()[field])>=2
    # Navigating elsewhere does not clear a login or mutate a selection.
    created=client.post('/api/spaces',json={'label':'Second workspace','idempotency_key':'new-workspace-page-0001'})
    assert created.status_code==201,created.text
    assert client.get('/api/spaces/'+created.json()['id']).status_code==200
    assert len(client.get('/api/conversations').json()['conversations'])==2
    assert 'current_space_id' not in client.get('/api/iam/me').json()


def test_project_shell_editor_offers_parent_source_not_just_current_subset(web):
    client,app=web
    app.state.store.execute('INSERT INTO project_shell_settings(project_id,settings) VALUES(?,?)',
        ('project',json.dumps({'mode':'custom','default_device_id':'a','allowed_device_ids':['a']})))
    result=client.get('/api/projects/project/shell')
    assert result.status_code==200,result.text
    data=result.json()
    assert {d['id'] for d in data['devices']}=={'a','b'},data
    result=client.put('/api/projects/project/shell',json={'settings':{'mode':'custom','default_device_id':'b'},
        'expected_version':data['version'],'idempotency_key':'project-shell-change-001'})
    assert result.status_code==200,result.text
    bad=client.put('/api/projects/project/shell',json={'settings':{'mode':'custom','default_device_id':'c'},
        'expected_version':result.json()['version'],'idempotency_key':'project-shell-outside-001'})
    assert bad.status_code in {400,403,409},bad.text


def test_workspace_membership_revocation_hides_old_metadata_without_logout(web):
    client,app=web
    p=profile(client);created=connection(client,p['id'])
    rpc(client,created['token'],'get_profile',meta={'openai/session':'bound-to-workspace'})
    assert client.get('/api/conversations').json()['conversations']
    app.state.store.execute("UPDATE memberships SET active=0 WHERE space_id='alice' AND user_id='alice'")
    assert client.get('/api/session').status_code==200
    for url,field in [('/api/conversations','conversations'),('/api/audit/sessions','sessions'),('/api/call-log','operations')]:
        response=client.get(url);assert response.status_code==200,response.text
        assert response.json()[field]==[]
