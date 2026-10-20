"""Fresh multi-user workspace HTTP fixtures, never old-API adapters.

Browser credentials identify accounts only. Every scoped URL is supplied by the
test; no header, cookie or request rewrite selects an ambient workspace.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub import iam
from hub.permissions import owner_policy
from shared.crypto import password_hash


def must(response, status=None):
    assert response.status_code == status if status is not None else response.is_success, response.text
    return response.json()


@dataclass
class Browser:
    client: TestClient
    cookie: str
    csrf: str
    space: str = 'team'

    def request(self, method, path, **kwargs):
        headers = {'Cookie': 'rd_session='+self.cookie, 'X-RD-CSRF': self.csrf,
                   'Origin': 'http://testserver'}
        headers.update(kwargs.pop('headers', {}))
        return self.client.request(method, path, headers=headers, **kwargs)

    def get(self, path, **kwargs): return self.request('GET', path, **kwargs)
    def post(self, path, **kwargs): return self.request('POST', path, **kwargs)
    def put(self, path, **kwargs): return self.request('PUT', path, **kwargs)
    def patch(self, path, **kwargs): return self.request('PATCH', path, **kwargs)
    def delete(self, path, **kwargs): return self.request('DELETE', path, **kwargs)


def call(browser, token, name='get_access_context', arguments=None, *, endpoint='/mcp'):
    return browser.post(endpoint, headers={'Authorization': 'Bearer '+token,
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'},
        json={'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'tools/call',
              'params': {'name': name, 'arguments': arguments or {}}})


def data(response):
    value = must(response)
    assert 'error' not in value and not value['result'].get('isError'), value
    return value['result']['structuredContent']


def set_member(owner, user, policy, *, space='team', active=True):
    members = must(owner.get(f'/api/spaces/{space}/members'))['members']
    current = next((m for m in members if m['user_id']==user and m['source']=='manual'), None)
    return must(owner.put(f'/api/spaces/{space}/members/{user}', json={
        'policy': policy, 'active': active, 'expected_version': current['version'] if current else 0,
        'idempotency_key': uuid.uuid4().hex}))


def make_profile(owner, policy, *, space='team', label=None):
    return must(owner.post(f'/api/spaces/{space}/profiles', json={
        'label': label or 'Fixture '+uuid.uuid4().hex, 'policy': policy,
        'idempotency_key': uuid.uuid4().hex}))


def update_profile(owner, profile, policy, *, space='team', enabled=True):
    return must(owner.put(f'/api/spaces/{space}/profiles/{profile["id"]}', json={
        'label': profile['label'], 'policy': policy, 'enabled': enabled,
        'expected_version': profile['version'], 'idempotency_key': uuid.uuid4().hex}))


def make_connection(browser, profile, *, space='team', live=False, external=False):
    return must(browser.post(f'/api/spaces/{space}/connections', json={
        'name': 'Test connection', 'profile_id': profile['id'], 'profile_version': profile['version'],
        'consent_mode': 'live' if live else 'fixed', 'confirm_live_profile': live,
        'confirm_external_mcp': external, 'idempotency_key': uuid.uuid4().hex}), 201)


def project_policy(*actions, manage=()):
    return {'project_rules': [{'projects': ['project-team'], 'actions': list(actions or ('read',))}],
            'manage': list(manage)}


def pending_oauth(browser, *, resource='http://testserver/mcp'):
    """Register a real public client and prepare only the new two-stage flow."""
    redirect='http://localhost:12345/callback'
    client=must(browser.post('/oauth/register',json={
        'client_name':'Workspace fixture','redirect_uris':[redirect],
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],
        'token_endpoint_auth_method':'none'}))
    verifier=uuid.uuid4().hex+uuid.uuid4().hex
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response=browser.get('/oauth/authorize',params={
        'client_id':client['client_id'],'redirect_uri':redirect,'response_type':'code',
        'scope':'codepier.workspace','resource':resource,'code_challenge':challenge,
        'code_challenge_method':'S256','state':'fixture-client-state'},follow_redirects=False)
    assert response.status_code==303,response.text
    location=response.headers['location']
    query=parse_qs(urlparse(location).query)
    identifier=query['authorize'][0]
    return {'id':identifier,'client_id':client['client_id'],'redirect_uri':redirect,
            'resource':resource,'verifier':verifier}


def select_oauth_space(browser, pending, *, space='team'):
    path='/api/oauth/requests/'+pending['id']
    details=must(browser.get(path))
    return must(browser.post(path+'/select-space',json={
        'space_id':space,'selection_version':details['selection_version']}))


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    app = create_app(str(tmp_path/'hub'))
    store, sessions, now = app.state.store, {}, time.time()
    with store.transaction():
        for user in ('owner', 'alice', 'bob'):
            store.db.execute('INSERT INTO users VALUES(?,?,?,?)',
                (user, user, password_hash('fixture-password-only'), now))
            iam.create_personal_space(store, user, user+' personal')
            sessions[user] = app.state.auth.new_session(user)
        for space in ('team', 'other-team'):
            store.db.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,?)',
                             (space, 'Development '+space, 'owner', now))
            store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)',
                             (space, 'owner', owner_policy().model_dump_json()))
            device = 'device-'+space
            root = tmp_path/space
            root.mkdir()
            store.db.execute('INSERT INTO devices(id,name,secret,owner_user_id,info,created) VALUES(?,?,?,?,?,?)',
                (device, device, store.encrypt('fixture-device-secret-'+'x'*32), 'owner',
                 json.dumps({'shell_enabled': True}), now))
            store.db.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)', (space,device))
            from tests.current_model import CurrentModel
            CurrentModel(store).folder('folder-'+space,space=space,device=device,path=root)
            store.db.execute('''INSERT INTO projects(id,space_id,alias,alias_key,source_id,subpath,owner_user_id,created)
                VALUES(?,?,'same-alias','same-alias',?,'.','owner',?)''', ('project-'+space,space,'folder-'+space,now))
        for user in ('alice', 'bob'):
            store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)', ('team',user,'{}'))
    with TestClient(app, raise_server_exceptions=True) as client:
        browsers = {user: Browser(client, info['cookie'], info['csrf']) for user,info in sessions.items()}
        browsers['other'] = Browser(client, sessions['owner']['cookie'], sessions['owner']['csrf'], 'other-team')
        yield app, browsers
