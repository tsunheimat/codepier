"""Disposable HTTP/IAM fixture and fake MCP server for external adapters."""
from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from hub import iam
from hub.app import create_app
from hub.gateway.remote import RemotePool
from tests.current_model import CurrentModel
from tests.identity_fixtures import set_member
from shared.mcp_protocol import MODERN
from shared.crypto import password_hash


@dataclass
class Browser:
    client: TestClient
    cookie: str
    csrf: str

    def request(self, method, path, **kwargs):
        headers = {'Cookie': 'rd_session=' + self.cookie, 'X-RD-CSRF': self.csrf}
        headers.update(kwargs.pop('headers', {}))
        return self.client.request(method, path, headers=headers, **kwargs)

    def get(self, path, **kwargs): return self.request('GET', path, **kwargs)
    def post(self, path, **kwargs): return self.request('POST', path, **kwargs)
    def put(self, path, **kwargs): return self.request('PUT', path, **kwargs)
    def patch(self, path, **kwargs): return self.request('PATCH', path, **kwargs)
    def delete(self, path, **kwargs): return self.request('DELETE', path, **kwargs)


def must(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def rpc(browser, token, method, params):
    response = browser.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'})
    return must(response)['result']


def call(browser, token, name, arguments, key=None):
    params = {'name': name, 'arguments': arguments}
    if key:
        params['_meta'] = {'codepier/idempotencyKey': key}
    return rpc(browser, token, 'tools/call', params)


def code(result):
    assert result.get('isError'), result
    return json.loads(result['content'][0]['text'])['error']['code']


class Backend:
    def __init__(self):
        properties = {'project_id': {'type': 'integer'}, 'pipeline_id': {'type': 'integer'},
                      'page': {'type': 'integer'}, 'per_page': {'type': 'integer'}}
        self.tools = [
            {'name': 'backend_pipeline', 'inputSchema': {'type': 'object', 'additionalProperties': False,
                'properties': {k: properties[k] for k in ('project_id', 'pipeline_id')}, 'required': ['project_id', 'pipeline_id']}},
            {'name': 'backend_list', 'inputSchema': {'type': 'object', 'additionalProperties': False,
                'properties': {k: properties[k] for k in ('project_id', 'page', 'per_page')}, 'required': ['project_id', 'per_page']}},
        ]
        self.calls = []
        self.on_initialize = None
        self.on_call = None
        self.fail_after_call = False
        self.result_override = None

    async def handle(self, request):
        message = json.loads(request.content)
        method, rid = message['method'], message.get('id')
        if method == 'server/discover':
            result = {'supportedVersions': [MODERN], 'capabilities': {'tools': {}}}
            if self.on_initialize:
                self.on_initialize()
        elif method == 'initialize':
            result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'fake', 'version': '1'}, 'capabilities': {'tools': {}}}
            if self.on_initialize:
                self.on_initialize()
        elif method == 'notifications/initialized':
            return httpx.Response(202, request=request)
        elif method == 'tools/list':
            result = {'tools': copy.deepcopy(self.tools)}
        elif method == 'tools/call':
            args = message['params']['arguments']
            self.calls.append({'name': message['params']['name'], 'arguments': args, 'authorization': request.headers.get('authorization')})
            if self.on_call:
                self.on_call()
            if self.fail_after_call:
                raise httpx.ReadTimeout('fixture uncertain result', request=request)
            result = self.result_override or {'content': [{'type': 'text', 'text': 'fixture result'}], 'structuredContent': {'arguments': args}}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': rid, 'result': {**result, 'resultType': 'complete'}}, request=request)


async def public_dns(host, port):
    return ['93.184.216.34']


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    monkeypatch.setenv('HUB_BUILTIN_AGENT_ENABLED', 'false')
    monkeypatch.delenv('CODEPIER_EXTERNAL_PLUGIN_DIR', raising=False)
    app = create_app(str(tmp_path / 'hub'))
    store = app.state.store
    model=CurrentModel(store);sessions={}
    for user in ('owner','viewer','other'):
        model.user(user,instance_admin=user=='owner',
            password_hash=password_hash('isolated-plugin-browser-password'))
        sessions[user]=model.session(user=user)
    model.workspace('team',owner='owner',label='Plugin tests')
    for user in ('viewer','other'):model.member('team',user,{})
    model.device('device',owner='owner');model.bind_device('team','device')
    root=tmp_path/'source';root.mkdir()
    model.folder('plugin-folder',space='team',device='device',path=root)
    for project in ('project-a','project-b'):
        model.project(project,space='team',folder='plugin-folder',owner='owner',subpath=project)
    backend = Backend()
    app.state.gateway.enabled = True
    app.state.gateway.pool = RemotePool(resolver=public_dns, transport=httpx.MockTransport(backend.handle))
    with TestClient(app, raise_server_exceptions=True) as client:
        browsers = {u: Browser(client, info['cookie'], info['csrf']) for u, info in sessions.items()}
        yield app, browsers, backend


def setup(plugin_env, *, mode='live', consent=True, actions=None, projects=None):
    app, b, backend = plugin_env
    connector = must(b['owner'].post('/api/mcp-gateway/connectors', json={'label': 'Fixture', 'endpoint': 'https://mcp.example/mcp'}), 201)
    account = must(b['owner'].post('/api/spaces/team/mcp-gateway/accounts', json={'connector_id': connector['id'], 'label': 'Shared fixture',
        'sharing': 'space', 'token': 'FIXTURE_BACKEND_CREDENTIAL'}), 201)
    discovery = must(b['owner'].post('/api/spaces/team/mcp-gateway/accounts/' + account['id'] + '/discover'))
    binding = must(b['owner'].post('/api/spaces/team/mcp-gateway/bindings', json={'account_id': account['id'], 'alias': 'rawgitlab',
        'catalog_hash': discovery['catalog_hash'], 'tools': [t['name'] for t in discovery['tools']], 'confirmed': True}), 201)
    manifest = must(b['owner'].get('/api/external-plugins'))['plugins'][0]
    integration_body = {'plugin_id': 'gitlab', 'manifest_hash': manifest['manifest_hash'], 'expected_version': 0,
        'backends': {'gitlab': {'binding_id': binding['id'], 'tools': {'pipeline_get': 'backend_pipeline', 'pipeline_list': 'backend_list'}}}}
    integration = must(b['owner'].put('/api/spaces/team/external-integrations/main', json=integration_body))
    for project, external in [('project-a', 123), ('project-b', 456)]:
        must(b['owner'].put(f"/api/spaces/team/external-integrations/{integration['id']}/projects/{project}",
            json={'expected_version': 0, 'config': {'remote_project_id': external}}))
    policy={'project_rules':[{'projects':['project-a','project-b'],'actions':['read']}],
        'plugin_rules':[{'integration_id':integration['id'],'projects':projects or ['project-a'],
            'actions':actions or ['pipeline.get']}]}
    set_member(b['owner'],'viewer',policy)
    profile=must(b['owner'].post('/api/spaces/team/profiles',json={
        'label':'Plugin reader','policy':policy,'idempotency_key':uuid.uuid4().hex}))
    grant=create_connection(b['viewer'],profile,mode=mode)
    value={'app':app,'b':b,'backend':backend,'integration':integration,'integration_body':integration_body,
        'binding':binding,'account':account,'profile':profile,'grant':grant}
    if consent:
        approve(value)
    return value


def approve(value):
    browser, grant = value['b']['viewer'], value['grant']
    path = '/api/spaces/team/external-integrations/grants/' + grant['connection_id'] + '/consent'
    preview = must(browser.get(path))
    assert preview['routes'], preview
    must(browser.post(path, json={'confirmed': True, 'expected_profile_version': preview['profile_version'], 'review_hash': preview['review_hash']}))
    return preview


def create_connection(browser,profile,*,mode='live'):
    assert mode in {'live','fixed'}
    return must(browser.post('/api/spaces/team/connections',json={
        'name':'Plugin client','profile_id':profile['id'],'profile_version':profile['version'],
        'consent_mode':mode,'confirm_live_profile':mode=='live','confirm_external_mcp':True,
        'idempotency_key':uuid.uuid4().hex}),201)


def update_policy(value,**updates):
    old=must(value['b']['owner'].get('/api/spaces/team/profiles/'+value['profile']['id']))
    policy={**old['policy'],**updates}
    # These are two real distinct administrator actions, not a Role shim.
    # The fixture intentionally grants the human and the client the same pairs.
    set_member(value['b']['owner'],'viewer',policy)
    value['profile']=must(value['b']['owner'].put('/api/spaces/team/profiles/'+old['id'],json={
        'label':old['label'],'enabled':old['enabled'],'policy':policy,
        'expected_version':old['version'],'idempotency_key':uuid.uuid4().hex}))
    return value['profile']


def invoke(value, arguments=None, name='ext.main.pipeline.get', key=None):
    return call(value['b']['viewer'], value['grant']['token'], name,
        {'project': 'project-a', 'pipeline_id': 7} if arguments is None else arguments, key)
