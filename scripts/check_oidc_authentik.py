#!/usr/bin/env python3
"""Real isolated Authentik + Chromium + CodePier acceptance; no live credentials.

Starts its own uniquely named Compose project on a loopback port and temporary
Hub. Creates only disposable users/provider/resources. Never connects to a
configured production IdP. Reports contain checks, not passwords/codes/Tokens.
Requires Docker Compose, locked requirements-dev, and installed Chromium.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.workspace_contracts import WORKSPACE_SCOPE


def compose_command():
    """Match the supported developer setup, including standalone macOS Compose."""
    for command in (['docker', 'compose'], ['docker-compose']):
        if shutil.which(command[0]) and subprocess.run(
                [*command, 'version'], capture_output=True, timeout=15).returncode == 0:
            return command
    raise RuntimeError('Docker Compose v2 is required for isolated OIDC acceptance')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def required(response, status=200):
    if response.status_code != status:
        # Never echo a provider/token response body or callback query.
        raise AssertionError(f'{response.request.method} {response.request.url.path}: expected {status}, got {response.status_code}')
    return response.json() if response.content else {}


def verify_bootstrap_workspace(client):
    """Inspect the real bootstrap workspace without changing any ambient state."""
    me=required(client.get('/api/iam/me'))
    spaces=required(client.get('/api/spaces'))['spaces']
    personal=[space for space in spaces if space['kind']=='personal' and space['owner_user_id']==me['id']]
    assert me['instance_admin'] and len(personal)==1
    assert not {'default_space_id','current_space_id','active_space_id'} & me.keys()
    assert all(space['kind']!='legacy' for space in spaces)
    assert 'X-CodePier-Space' not in client.headers
    return personal[0]['id']



MCP_ACCEPTANCE_VERSION = '2025-11-25'


def mcp_request(client, access_token, method, params=None, *, notification=False):
    """Use the negotiated Streamable HTTP contract, including both media types.

    Keep this explicit rather than relying on a panel client's default headers.
    HTTP/authentication failures remain failures; do not retry or mask a 406.
    """
    body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
    if not notification:
        body['id'] = uuid.uuid4().hex
    return client.post('/mcp', headers={
        'Authorization': 'Bearer ' + access_token,
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
        'MCP-Protocol-Version': MCP_ACCEPTANCE_VERSION,
    }, json=body)


def initialize_mcp(client, access_token):
    reply = required(mcp_request(client, access_token, 'initialize', {
        'protocolVersion': MCP_ACCEPTANCE_VERSION, 'capabilities': {},
        'clientInfo': {'name': 'codepier-oidc-acceptance', 'version': '1'},
    }))
    assert reply['result']['protocolVersion'] == MCP_ACCEPTANCE_VERSION
    required(mcp_request(client, access_token, 'notifications/initialized', notification=True), 202)


def await_ready(predicate, seconds=300):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError('Disposable service readiness deadline exceeded')


def password_hash(password):
    salt = secrets.token_hex(12)
    count = 1000000
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), count)
    return f'pbkdf2_sha256${count}${salt}${base64.b64encode(key).decode()}'


def acceptance(output):
    result = {'provider_version': '2026.8.3', 'production_changed': False,
              'checks': [], 'status': 'running'}
    phase = 'startup'
    secret_values = []
    page = None
    browser_diagnostics = {}
    def secret():
        value = secrets.token_urlsafe(36)
        secret_values.append(value)
        if os.getenv('GITHUB_ACTIONS') == 'true':
            print('::add-mask::' + value, flush=True)
        return value
    def passed(name):
        result['checks'].append(name)
        print('PASS ' + name, flush=True)
    with tempfile.TemporaryDirectory(prefix='codepier-oidc-acceptance-') as temporary:
        folder = Path(temporary)
        env = {**os.environ, 'TEST_DB_PASSWORD': secret(), 'TEST_AUTHENTIK_SECRET': secret(),
               'TEST_BOOTSTRAP_TOKEN': secret(), 'TEST_BOOTSTRAP_HASH': password_hash(secret()),
               'TEST_AUTHENTIK_PORT': str(free_port())}
        project = 'codepier-oidc-test-' + uuid.uuid4().hex[:12]
        compose = [*compose_command(), '-f', str(ROOT/'tests/fixtures/oidc-authentik/compose.yml'), '-p', project]
        def docker(*args, check=True):
            return subprocess.run([*compose, *args], env=env, capture_output=True, text=True,
                                  timeout=360, check=check)
        hub = None
        logs = (folder/'services.log').open('w+')
        try:
            phase = 'authentik-bootstrap'
            docker('up', '-d')
            issuer_base = 'http://127.0.0.1:' + env['TEST_AUTHENTIK_PORT']
            ak = httpx.Client(base_url=issuer_base, headers={'Authorization': 'Bearer ' + env['TEST_BOOTSTRAP_TOKEN']}, timeout=20)
            await_ready(lambda: ak.get('/api/v3/core/users/me/').status_code == 200)
            def ak_call(method, path, body=None, status=200):
                return required(ak.request(method, '/api/v3/' + path, json=body), status)
            # API authentication becomes ready before worker blueprints finish.
            # Wait for actual required configuration, not an arbitrary sleep or
            # an assumption that the built-in signing certificate is exposed.
            phase = 'authentik-default-flows'
            flow_slugs = {'default-provider-authorization-implicit-consent',
                          'default-provider-invalidation-flow', 'default-authentication-flow'}
            def flows_ready():
                rows = ak_call('GET', 'flows/instances/?page_size=100')['results']
                return flow_slugs <= {row['slug'] for row in rows}
            await_ready(flows_ready)
            flows = {row['slug']: row['pk'] for row in ak_call('GET', 'flows/instances/?page_size=100')['results']}
            authorization = flows['default-provider-authorization-implicit-consent']
            invalidation = flows['default-provider-invalidation-flow']
            authentication = flows['default-authentication-flow']
            phase = 'authentik-signing-key'
            signing_key = ak_call('POST', 'crypto/certificatekeypairs/generate/', {
                'common_name': 'codepier-acceptance-signing', 'validity_days': 1})
            assert signing_key['private_key_available'] is True
            signing = signing_key['pk']
            phase = 'authentik-scope-mappings'
            scope_names = {'openid', 'profile', 'email', 'offline_access'}
            def scopes_ready():
                rows = ak_call('GET', 'propertymappings/provider/scope/?page_size=100')['results']
                return scope_names <= {row.get('scope_name') for row in rows}
            await_ready(scopes_ready)
            mappings = ak_call('GET', 'propertymappings/provider/scope/?page_size=100')['results']
            scopes = [row['pk'] for row in mappings if row.get('scope_name') in scope_names]
            phase = 'authentik-test-users'
            group = ak_call('POST', 'core/groups/', {'name': 'codepier-acceptance-team'}, 201)
            users = []
            for name in ('alice', 'bob'):
                password = secret()
                u = ak_call('POST', 'core/users/', {'username': 'codepier-test-' + name, 'name': name.title(),
                            'is_active': True, 'groups': [group['pk']], 'email': name+'@example.test'}, 201)
                ak_call('POST', f"core/users/{u['pk']}/set_password/", {'password': password}, 204)
                users.append((u, password))
            passed('isolated_provider_and_two_ordinary_users')
            phase = 'hub-bootstrap'
            hub_port = free_port()
            hub_url = f'http://127.0.0.1:{hub_port}'
            hub_dir = folder/'hub'
            hub_env = {**os.environ, 'CODEPIER_ADMIN_PASSWORD': secret(), 'HUB_PUBLIC_URL': hub_url,
                       'MCP_PUBLIC_URL': hub_url, 'CODEPIER_OIDC_INSECURE_TEST_LOOPBACK': '1'}
            subprocess.run([sys.executable, '-m', 'hub', '--data-dir', str(hub_dir), 'init'], cwd=ROOT,
                           env=hub_env, stdout=logs, stderr=logs, check=True, timeout=30)
            hub = subprocess.Popen([sys.executable, '-m', 'hub', '--data-dir', str(hub_dir), 'run', '--host',
                                    '127.0.0.1', '--port', str(hub_port)], cwd=ROOT, env=hub_env, stdout=logs, stderr=logs)
            owner = httpx.Client(base_url=hub_url, timeout=30)
            await_ready(lambda: owner.get('/healthz').status_code == 200, 60)
            login = required(owner.post('/api/login', json={'username': 'admin', 'password': hub_env['CODEPIER_ADMIN_PASSWORD']}))
            owner.headers['X-RD-CSRF'] = login['csrf']
            verify_bootstrap_workspace(owner)
            passed('fresh_admin_has_workspace_without_ambient_state')
            sid=required(owner.post('/api/spaces',json={'label':'Provider acceptance team',
                'idempotency_key':uuid.uuid4().hex}),201)['id']
            resource_policy={'project_rules':[{'actions':['read'],'all_projects':True}]}
            client_id, client_secret = 'codepier-acceptance', secret()
            provider_config = {'label': 'Isolated Authentik', 'issuer': issuer_base+'/application/o/codepier-test/',
                               'client_id': client_id, 'client_secret': client_secret, 'enabled': True,
                               'admission': 'jit', 'scopes': 'openid profile email offline_access', 'freshness_seconds': 300}
            provider = required(owner.post('/api/iam/oidc/providers', json=provider_config), 201)
            callback = hub_url + '/auth/oidc/callback'
            ak_provider = ak_call('POST', 'providers/oauth2/', {
                'name': 'CodePier isolated test', 'authorization_flow': authorization, 'authentication_flow': authentication,
                'invalidation_flow': invalidation, 'client_type': 'confidential', 'client_id': client_id,
                'client_secret': client_secret, 'signing_key': signing, 'include_claims_in_id_token': True,
                # Authentik's API defaults grant_types to an empty list.
                # Explicitly permit only the code and refresh flows we test.
                'grant_types': ['authorization_code', 'refresh_token'],
                'redirect_uris': [{'matching_mode': 'strict', 'url': callback}], 'property_mappings': scopes,
            }, 201)
            assert set(ak_provider['grant_types']) == {'authorization_code', 'refresh_token'}
            ak_call('POST', 'core/applications/', {'name': 'CodePier isolated test', 'slug': 'codepier-test', 'provider': ak_provider['pk']}, 201)
            verified = required(owner.post('/api/iam/oidc/providers/'+provider['id']+'/check'))
            assert verified['issuer'] == provider_config['issuer'] and verified['callback'] == callback
            required(owner.post('/api/iam/oidc/providers/'+provider['id']+'/mappings', json={
                'group_name':group['name'],'space_id':sid,
                'policy':{**resource_policy,'manage':['profiles']}}),201)
            passed('actual_discovery_jwks_and_exact_callback')
            phase = 'real-browser-login'
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                human_clients, identities, profiles = [], [], []
                for user, password in users:
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(30000)
                    def capture_navigation(response):
                        # Status/path only; never persist authorization query,
                        # cookies, submitted credentials, or response bodies.
                        if response.request.is_navigation_request():
                            u = urlsplit(response.url)
                            browser_diagnostics['navigation'] = {'origin': u.scheme + '://' + u.netloc, 'path': u.path, 'status': response.status}
                            error = parse_qs(u.query).get('error', [''])[0]
                            if re.fullmatch(r'[a-z_]{1,64}', error):
                                browser_diagnostics['oauth_error'] = error
                    page.on('response', capture_navigation)
                    page.goto(hub_url)
                    page.locator('#oidc-login-buttons a').click()
                    try:
                        identification = page.locator('ak-stage-identification')
                        identification.locator('input[name="uidField"]').fill(user['username'])
                        identification.locator('button[type="submit"]').click()
                        # The identification screen has a password-manager
                        # helper input outside the real password stage. Wait
                        # for the actual stage instead of filling that helper
                        # while the previous stage's request is in flight.
                        password_stage = page.locator('ak-stage-password')
                        expect(password_stage).to_be_visible()
                        password_stage.locator('input[name="password"]').fill(password)
                        password_stage.locator('button[type="submit"]').click()
                        # The real provider may render a consent stage even
                        # for its implicit-consent flow (for example when a
                        # newly requested scope requires review). Complete the
                        # user's explicit submit; never forge a callback/token.
                        page.wait_for_function('''hub => {
                            if (location.origin === hub) return true;
                            const find = root => {
                                for (const node of root.querySelectorAll('*')) {
                                    if (node.tagName === 'AK-STAGE-CONSENT') return true;
                                    if (node.shadowRoot && find(node.shadowRoot)) return true;
                                }
                                return false;
                            };
                            return find(document);
                        }''', arg=hub_url, timeout=60000)
                        if urlsplit(page.url).netloc != urlsplit(hub_url).netloc:
                            assert urlsplit(page.url).netloc == urlsplit(issuer_base).netloc
                            consent_stage = page.locator('ak-stage-consent')
                            expect(consent_stage).to_be_visible()
                            consent_stage.locator('button[name="continue"][type="submit"]').click()
                        page.wait_for_url(hub_url+'/**', timeout=60000)
                    except Exception:
                        u = urlsplit(page.url)
                        browser_diagnostics['page'] = {'origin': u.scheme + '://' + u.netloc, 'path': u.path}
                        # Read only static metadata through open shadow roots.
                        # Never inspect field values or page text, which could
                        # contain Tokens on an unexpected error page.
                        browser_diagnostics['elements'] = page.evaluate("""() => {const out=[];const visit=root=>{for(const node of root.querySelectorAll('*')){if(node.tagName==='INPUT')out.push({tag:node.tagName,name:node.name,type:node.type,id:node.id,invalid:node.getAttribute('aria-invalid')});else if(node.tagName.startsWith('AK-STAGE-'))out.push({tag:node.tagName});if(node.shadowRoot)visit(node.shadowRoot);}};visit(document);return out.slice(0,40);}""")
                        raise
                    page.goto(hub_url+'/#spaces')
                    expect(page.locator('#workspace-page')).to_be_visible(timeout=30000)
                    expect(page.locator('#iam-active-space')).to_have_count(0)
                    cookies = {c['name']: c['value'] for c in context.cookies(hub_url)}
                    client = httpx.Client(base_url=hub_url, cookies=cookies, timeout=30)
                    session = required(client.get('/api/session'))
                    client.headers.update({'X-RD-CSRF':session['csrf']})
                    me = required(client.get('/api/iam/me'))
                    assert not me['instance_admin'] and not me['local_login']
                    assert {s['kind'] for s in me['spaces']} == {'personal', 'workspace'}
                    assert all(s['id'] != 'legacy' for s in me['spaces'])
                    assert client.get('/api/iam/users').status_code == 403
                    # A non-admin Profile manager may configure only explicit
                    # resources, never delegate a future-resource wildcard.
                    # Exercise that real denial instead of weakening it for CI.
                    denied=client.post('/api/spaces/'+sid+'/profiles',json={
                        'label':user['name']+' denied wildcard','policy':resource_policy,
                        'idempotency_key':uuid.uuid4().hex})
                    assert denied.status_code==403 and denied.json()['error']['code']=='DELEGATION_EXCEEDED'
                    p=required(client.post('/api/spaces/'+sid+'/profiles',json={
                        'label':user['name']+' read profile','policy':{},
                        'idempotency_key':uuid.uuid4().hex}))
                    human_clients.append(client); identities.append(me); profiles.append(p)
                    context.close()
                assert identities[0]['id'] != identities[1]['id']
                # Profiles are workspace configuration; sharing them does not
                # merge the users' Connection identities or private receipts.
                assert human_clients[0].get('/api/spaces/'+sid+'/profiles/'+profiles[1]['id']).status_code==200
                passed('real_code_pkce_login_direct_group_policy_and_workspace_profiles')
                phase='downstream-workspace-consent'
                alice = human_clients[0]
                registration = required(alice.post('/oauth/register', json={'redirect_uris': ['http://localhost:19876/callback']}), 201)
                verifier = secrets.token_urlsafe(64)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                response = alice.get('/oauth/authorize', params={'response_type':'code','client_id':registration['client_id'],
                    'redirect_uri':'http://localhost:19876/callback','scope':WORKSPACE_SCOPE,'resource':hub_url+'/mcp',
                    'code_challenge_method':'S256','code_challenge':challenge})
                assert response.status_code == 303
                auth_path = response.headers['location']
                ctx = browser.new_context()
                ctx.add_cookies([{'name': 'rd_session', 'value': alice.cookies.get('rd_session'), 'url': hub_url}])
                page = ctx.new_page()
                page.route('http://localhost:19876/callback*', lambda route: route.fulfill(body='Authorized isolated test callback'))
                page.goto(hub_url+auth_path)
                expect(page.locator('[name="consent_profile"]')).to_be_disabled()
                page.select_option('[name="consent_space"]',sid)
                expect(page.locator('[name="consent_profile"]')).to_be_enabled()
                page.select_option('[name="consent_profile"]',profiles[0]['id'])
                page.select_option('[name="consent_mode"]','live')
                confirmation=page.locator('[name="confirm_live_profile"]')
                expect(confirmation).not_to_be_checked()
                confirmation.check();page.locator('[data-consent-allow]').click()
                page.wait_for_url('http://localhost:19876/callback*')
                code = parse_qs(urlsplit(page.url).query)['code'][0]
                tokens = required(alice.post('/oauth/token', data={'grant_type':'authorization_code','code':code,
                    'client_id':registration['client_id'],'redirect_uri':'http://localhost:19876/callback',
                    'resource':hub_url+'/mcp','code_verifier':verifier}))
                # MCP is a separate client: never rely on a human panel cookie
                # or X-CodePier-Space header to make downstream Tokens work.
                with httpx.Client(base_url=hub_url, timeout=30) as mcp:
                    initialize_mcp(mcp, tokens['access_token'])
                    def rpc(name, token_value=None, arguments=None):
                        return mcp_request(mcp, token_value or tokens['access_token'], 'tools/call',
                                           {'name': name, 'arguments': arguments or {}})
                    connection_id=required(rpc('get_profile'))['result']['structuredContent']['id']
                    access=required(rpc('get_access_context'))['result']['structuredContent']
                    assert access['profile']['id']==profiles[0]['id'] and connection_id!=profiles[0]['id']
                    peer=required(human_clients[1].post('/api/spaces/'+sid+'/connections',json={
                        'name':'Second private connection','profile_id':profiles[0]['id'],
                        'profile_version':profiles[0]['version'],'consent_mode':'live','confirm_live_profile':True,
                        'idempotency_key':uuid.uuid4().hex}),201)
                    assert required(rpc('get_profile',peer['token']))['result']['structuredContent']['id']!=connection_id
                    assert peer['connection_id'] not in json.dumps(required(alice.get('/api/spaces/'+sid+'/connections')))
                    passed('separate_oauth_issuer_two_stage_consent_and_private_connections')
                    phase = 'dynamic-resource-and-refresh'
                    # The workspace administrator explicitly publishes future
                    # read access after the human approved a live Profile.
                    # Both the current group membership and that consent still
                    # constrain the existing Connection; identity never changes.
                    reviewed=required(owner.put('/api/spaces/'+sid+'/profiles/'+profiles[0]['id'],json={
                        'label':profiles[0]['label'],'policy':resource_policy,
                        'expected_version':profiles[0]['version'],'idempotency_key':uuid.uuid4().hex}))
                    assert reviewed['id']==profiles[0]['id'] and reviewed['version']==profiles[0]['version']+1
                    passed('non_admin_wildcard_denial_and_explicit_admin_profile_publication')
                    # Seed only an isolated offline resource binding; never an
                    # old device/root Project or an executable credential.
                    db=sqlite3.connect(hub_dir/'hub.sqlite3',timeout=30)
                    db.execute('PRAGMA foreign_keys=ON')
                    source=folder/'offline-source';source.mkdir()
                    with db:
                        device='fixture-'+uuid.uuid4().hex;folder_id='folder-'+uuid.uuid4().hex
                        db.execute('INSERT INTO devices(id,name,secret,owner_user_id,created) VALUES(?,?,?,?,?)',
                            (device,'offline fixture','not-a-live-credential',identities[0]['id'],time.time()))
                        db.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)',(sid,device))
                        db.execute('''INSERT INTO space_folders(id,space_id,name,source_kind,device_id,device_path,mode)
                            VALUES(?,?,?,'device',?,?,'read')''',(folder_id,sid,'Isolated source',device,str(source)))
                        project_id='fixture-'+uuid.uuid4().hex
                        db.execute('''INSERT INTO projects(id,alias,alias_key,folder_id,subpath,space_id,owner_user_id,created)
                            VALUES(?,?,?,? ,'.',?,?,?)''',(project_id,'new-project','new-project',folder_id,sid,identities[0]['id'],time.time()))
                    projects=required(rpc('project_query',arguments={'operation':'list'}))['result']['structuredContent']['projects']
                    assert project_id in {p['id'] for p in projects}
                    fresh = required(alice.post('/oauth/token', data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],
                        'client_id':registration['client_id'],'resource':hub_url+'/mcp'}))
                    assert fresh['refresh_token'] != tokens['refresh_token']
                    assert required(rpc('get_profile',fresh['access_token']))['result']['structuredContent']['id']==connection_id
                    passed('explicit_live_profile_future_project_and_stable_refreshed_connection')
                    phase = 'real-group-removal'
                    ak_call('PATCH', f"core/users/{users[0][0]['pk']}/", {'groups': []})
                    with db:
                        # Advance only the fixture's reconciliation schedule, not its policy/results.
                        db.execute('UPDATE external_identities SET checked_at=0 WHERE user_id=?',(identities[0]['id'],))
                    required(owner.post('/api/iam/oidc/reconcile'))
                    assert rpc('workspace', arguments={'operation':'list'}).status_code == 403
                    assert rpc('workspace', fresh['access_token'], {'operation':'list'}).status_code == 403
                    assert all(s['id'] != sid for s in required(alice.get('/api/iam/me'))['spaces'])
                    assert any(s['id'] == sid for s in required(human_clients[1].get('/api/iam/me'))['spaces'])
                    passed('actual_idp_group_removal_revokes_team_and_existing_grant_only_for_target_user')
                    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
                    db.close()
                ctx.close();browser.close()
                for client in human_clients:client.close()
            owner.close();ak.close()
            result['status']='passed'
        except Exception as exc:
            result['status']='failed';result['phase']=phase
            if browser_diagnostics:result['browser'] = browser_diagnostics
            # Only type and sanitized bounded text; no raw provider responses/logs.
            detail=str(exc)
            for value in secret_values:detail=detail.replace(value,'[redacted]')
            result['error_type']=type(exc).__name__
            result['error']=re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', detail)[:1800]
            raise RuntimeError(f'Isolated Authentik acceptance failed in {phase}: {type(exc).__name__}') from None
        finally:
            if hub is not None:
                hub.terminate()
                try:hub.wait(timeout=15)
                except subprocess.TimeoutExpired:hub.kill();hub.wait(timeout=5)
            logs.close()
            docker('down','--volumes','--remove-orphans',check=False)
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/authentik-acceptance.json')
    args=parser.parse_args()
    acceptance(args.output)


if __name__=='__main__':main()
