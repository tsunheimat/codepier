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
from shared.crypto import password_hash
from shared.mcp_protocol import MODERN


@dataclass
class Browser:
    client: TestClient
    cookie: str
    csrf: str
    space: str = 'team'

    def request(self, method, path, **kwargs):
        headers = {'Cookie': 'rd_session=' + self.cookie, 'X-RD-CSRF': self.csrf, 'X-CodePier-Space': self.space}
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
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'})
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
    hashed, sessions = password_hash('fixture-password-only'), {}
    with store.transaction():
        for user in ('owner', 'viewer', 'other'):
            store.db.execute('INSERT INTO users VALUES(?,?,?,?)', (user, user, hashed, time.time()))
            iam.create_personal_space(store, user, user + ' personal')
            sessions[user] = app.state.auth.new_session(user)
        store.db.execute("INSERT INTO spaces(id,label,kind,created) VALUES('team','Plugin tests','team',?)", (time.time(),))
        for user in sessions:
            store.db.execute('INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,?)', ('team', user, 'owner' if user == 'owner' else 'member'))
        store.db.execute('INSERT INTO devices(id,name,secret,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?)',
            ('device', 'fixture', store.encrypt('x' * 43), 'team', 'owner', time.time()))
        for project in ('project-a', 'project-b'):
            store.db.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (project, project, project, 'device', str(tmp_path / project), 'write', 1, 'team', 'owner', time.time()))
    backend = Backend()
    app.state.gateway.enabled = True
    app.state.gateway.pool = RemotePool(resolver=public_dns, transport=httpx.MockTransport(backend.handle))
    with TestClient(app, raise_server_exceptions=True) as client:
        browsers = {u: Browser(client, info['cookie'], info['csrf']) for u, info in sessions.items()}
        yield app, browsers, backend


def setup(plugin_env, *, mode='role', consent=True, actions=None, projects=None):
    app, b, backend = plugin_env
    connector = must(b['owner'].post('/api/mcp-gateway/connectors', json={'label': 'Fixture', 'endpoint': 'https://mcp.example/mcp'}), 201)
    account = must(b['owner'].post('/api/mcp-gateway/accounts', json={'connector_id': connector['id'], 'label': 'Shared fixture',
        'sharing': 'space', 'token': 'FIXTURE_BACKEND_CREDENTIAL'}), 201)
    discovery = must(b['owner'].post('/api/mcp-gateway/accounts/' + account['id'] + '/discover'))
    binding = must(b['owner'].post('/api/mcp-gateway/bindings', json={'account_id': account['id'], 'alias': 'rawgitlab',
        'catalog_hash': discovery['catalog_hash'], 'tools': [t['name'] for t in discovery['tools']], 'confirmed': True}), 201)
    manifest = must(b['owner'].get('/api/external-plugins'))['plugins'][0]
    integration_body = {'plugin_id': 'gitlab', 'manifest_hash': manifest['manifest_hash'], 'expected_version': 0,
        'backends': {'gitlab': {'binding_id': binding['id'], 'tools': {'pipeline_get': 'backend_pipeline', 'pipeline_list': 'backend_list'}}}}
    integration = must(b['owner'].put('/api/external-integrations/main', json=integration_body))
    for project, external in [('project-a', 123), ('project-b', 456)]:
        must(b['owner'].put(f"/api/external-integrations/{integration['id']}/projects/{project}",
            json={'expected_version': 0, 'config': {'remote_project_id': external}}))
    role = must(b['owner'].post('/api/access-roles', json={'label': 'Plugin reader', 'idempotency_key': uuid.uuid4().hex,
        'project_rules': [{'projects': ['project-a', 'project-b'], 'actions': ['read']}],
        'plugin_rules': [{'integration_id': integration['id'], 'projects': projects or ['project-a'], 'actions': actions or ['pipeline.get']}]}), 201)
    must(b['owner'].put('/api/iam/spaces/team/assignments/' + role['id'] + '/viewer',
        json={'active': True, 'may_delegate': True, 'expected_version': 0}))
    grant = must(b['viewer'].post('/api/client-connections', json={'name': 'Plugin client', 'role_id': role['id'],
        'expected_role_version': role['version'], 'authorization_mode': mode, 'confirm_dynamic_role': mode == 'role',
        'confirm_external_mcp': True, 'idempotency_key': uuid.uuid4().hex}), 201)
    value = {'app': app, 'b': b, 'backend': backend, 'integration': integration, 'integration_body': integration_body,
             'binding': binding, 'account': account, 'role': role, 'grant': grant}
    if consent:
        approve(value)
    return value


def approve(value):
    browser, grant = value['b']['viewer'], value['grant']
    path = '/api/external-integrations/grants/' + grant['grant_id'] + '/consent'
    preview = must(browser.get(path))
    assert preview['routes'], preview
    must(browser.post(path, json={'confirmed': True, 'expected_role_version': preview['role_version'], 'review_hash': preview['review_hash']}))
    return preview


def update_role(value, **updates):
    old = must(value['b']['owner'].get('/api/access-roles/' + value['role']['id']))
    body = {k: old[k] for k in ('label', 'enabled', 'project_rules', 'device_rules', 'connector_rules', 'plugin_rules', 'vps_rules')}
    value['role'] = must(value['b']['owner'].put('/api/access-roles/' + old['id'], json={**body, 'expected_version': old['version'], **updates}))
    return value['role']


def invoke(value, arguments=None, name='ext.main.pipeline.get', key=None):
    return call(value['b']['viewer'], value['grant']['token'], name,
        {'project': 'project-a', 'pipeline_id': 7} if arguments is None else arguments, key)
