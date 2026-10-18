"""Real browser acceptance for explicit workspaces, not DOM/template simulations.

Every test uses a disposable Hub/Runner and synthetic session cookies. No live
workspace, browser profile, SSH host, or user credential is accessed.
"""
from __future__ import annotations

import base64
import hashlib
import re
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from tests.workspace_stack import workspace_stack

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.fixture
def workspace_server(tmp_path):
    with workspace_stack(tmp_path / 'browser-workspace') as server:
        yield server


@pytest.fixture(params=['chromium', 'webkit'])
def workspace_browser(request, chat_browser_pool, workspace_server):
    browser = chat_browser_pool(request.param)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    context.add_cookies([
        {'name': cookie.name, 'value': cookie.value, 'url': workspace_server.url,
         'httpOnly': True, 'sameSite': 'Lax'}
        for cookie in workspace_server.client.cookies.jar
    ])
    errors = []
    context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
    try:
        yield context, errors
    finally:
        context.close()


def open_workspace(context, server, path):
    page = context.new_page()
    page.goto(server.url + '/#' + path)
    expect(page.locator('#workspace-page')).to_be_visible(timeout=15000)
    return page


def test_two_tabs_do_not_share_current_workspace(workspace_server, workspace_browser):
    server = workspace_server
    context, errors = workspace_browser
    first = open_workspace(context, server, 'spaces/alice/profiles')
    expect(first.locator('#workspace-page h2')).to_have_text('Profiles')
    second = open_workspace(context, server, 'spaces')
    second.get_by_role('button', name='新建工作空间', exact=True).click()
    form = second.locator('#workspace-form')
    form.locator('[name="label"]').fill('Independent workspace')
    second.get_by_role('button', name='保存', exact=True).click()
    expect(second).to_have_url(re.compile(r'#spaces/(?!alice)[^/]+$'), timeout=15000)
    expect(second.locator('#workspace-page h1')).to_contain_text('Independent workspace')
    expect(first).to_have_url(re.compile(r'#spaces/alice/profiles$'))
    expect(first.locator('#workspace-page h2')).to_have_text('Profiles')
    me = server.must(server.client.get('/api/iam/me'))
    assert not {'current_space_id', 'active_space_id', 'space_id'} & me.keys()
    expect(first.get_by_text('当前空间', exact=True)).to_have_count(0)
    assert errors == []


def test_workspace_navigation_executes_real_page_scripts(workspace_server, workspace_browser):
    server = workspace_server
    context, errors = workspace_browser
    page = open_workspace(context, server, 'spaces/alice')
    for tab, heading in [
        ('folders', 'Folders'), ('storage', 'Shared Storage'), ('devices', 'Devices'),
        ('vps', 'VPS'), ('shell', 'Shell 继承'), ('profiles', 'Profiles'),
        ('connections', 'MCP Connections'), ('members', '成员与权限'),
        ('projects', 'Projects'),
    ]:
        page.locator(f'.product-tabs a[href="#spaces/alice/{tab}"]').click()
        expect(page).to_have_url(re.compile('#spaces/alice/' + tab + '$'))
        expect(page.locator('#workspace-page h2')).to_have_text(heading, timeout=10000)
    assert errors == []


def test_project_shell_form_only_offers_its_folder_devices(workspace_server, workspace_browser):
    server = workspace_server
    context, errors = workspace_browser
    page = open_workspace(context, server, 'spaces/alice/projects')
    page.locator('[data-project-detail="project"]').click()
    page.locator('[data-ws-project-shell]').click()
    form = page.locator('#workspace-form')
    expect(form).to_be_visible()
    form.locator('[name="mode"]').select_option('custom')
    choices = form.locator('[name="default_device_id"] option').evaluate_all(
        'nodes => nodes.map(node => node.value).filter(Boolean)')
    assert set(choices) == {'a', 'b'}
    assert 'c' not in choices
    form.locator('[name="default_device_id"]').select_option('a')
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    result = server.must(server.client.get('/api/projects/project/shell'))
    assert result['settings']['mode'] == 'custom'
    assert result['settings']['default_device_id'] == 'a'
    rejected = server.client.put('/api/projects/project/shell', json={
        'expected_version': result['version'], 'idempotency_key': uuid.uuid4().hex,
        'settings': {'mode': 'custom', 'default_device_id': 'c', 'allowed_device_ids': []},
    })
    assert rejected.status_code == 403, rejected.text
    assert errors == []


def test_oauth_two_levels_create_profile_return_and_confirm(workspace_server, workspace_browser):
    server = workspace_server
    context, errors = workspace_browser
    other = server.must(server.client.post('/api/spaces', json={
        'label': 'Second consent workspace', 'idempotency_key': uuid.uuid4().hex}))
    existing = server.must(server.client.get('/api/spaces/alice/profiles'))['profiles'][0]
    client = server.must(server.client.post('/oauth/register', json={
        'client_name': 'Browser consent fixture', 'redirect_uris': ['https://client.example/callback'],
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
        'token_endpoint_auth_method': 'none'}))
    verifier = 'b'*64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response = server.client.get('/oauth/authorize', params={
        'client_id': client['client_id'], 'redirect_uri': 'https://client.example/callback',
        'response_type': 'code', 'scope': 'codepier.workspace', 'resource': server.url + '/mcp',
        'code_challenge_method': 'S256', 'code_challenge': challenge, 'state': 'browser-state'},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    # The fixture callback is handled locally, never requested from an external host.
    context.route('https://client.example/callback*', lambda route: route.fulfill(
        status=200, content_type='text/html', body='<p>Consent callback</p>'))
    page = context.new_page()
    page.goto(server.url + response.headers['location'])
    spaces = page.locator('[name="consent_space"]')
    profiles = page.locator('[name="consent_profile"]')
    allow = page.locator('[data-consent-allow]')
    expect(spaces).to_be_visible(timeout=15000)
    expect(profiles).to_be_disabled()
    expect(allow).to_be_disabled()
    spaces.select_option('alice')
    expect(profiles).to_be_enabled()
    profiles.select_option(existing['id'])
    expect(allow).to_be_enabled()
    spaces.select_option(other['id'])
    expect(profiles).to_be_disabled()
    expect(allow).to_be_disabled()
    expect(profiles.locator('option[value="'+existing['id']+'"]')).to_have_count(0)
    expect(spaces).to_be_enabled()
    spaces.select_option('alice')
    expect(profiles).to_be_enabled()
    expect(profiles).to_have_value('')
    before = server.must(server.client.get('/api/spaces/alice/connections'))['connections']
    page.locator('[data-consent-create]').click()
    expect(page.locator('#workspace-page')).to_be_visible()
    page.locator('[data-ws-action="new-profiles"]').click()
    form = page.locator('#workspace-form')
    form.locator('[name="label"]').fill('Created during consent')
    form.locator('[data-policy-kind="projects"][data-resource="project"][value="read"]').check()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    expect(profiles).to_be_enabled()
    expect(profiles.locator('option:checked')).to_have_text('Created during consent')
    selected = profiles.input_value()
    after = server.must(server.client.get('/api/spaces/alice/connections'))['connections']
    assert {c['id'] for c in before} == {c['id'] for c in after}
    expect(allow).to_be_enabled()
    allow.click()
    expect(page).to_have_url(re.compile(r'https://client.example/callback\?'), timeout=15000)
    query = parse_qs(urlparse(page.url).query)
    assert query['state'] == ['browser-state']
    token = server.must(server.client.post('/oauth/token', data={
        'grant_type': 'authorization_code', 'client_id': client['client_id'],
        'redirect_uri': 'https://client.example/callback', 'resource': server.url + '/mcp',
        'code': query['code'][0], 'code_verifier': verifier}))
    identity = server.mcp('get_access_context', token=token['access_token'])
    assert identity['profile']['id'] == selected
    assert identity['connection']['id'] != selected
    assert errors == []


@pytest.mark.parametrize('mode', ['top_level', 'project_only', 'both'])
def test_vps_display_form_and_mcp_share_one_resource(workspace_server, workspace_browser, mode):
    server = workspace_server
    context, errors = workspace_browser
    page = open_workspace(context, server, 'spaces/alice/vps')
    page.locator('[data-ws-action="vps"][data-id="'+'a'*32+'"]').click()
    form = page.locator('#workspace-form')
    form.locator('[name="display_mode"]').select_option(mode)
    if mode != 'top_level':
        form.locator('[name="projects"][value="project"]').check()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    top = server.mcp('vps')['vps']
    scoped = server.mcp('vps', {'project': 'codepier'})['vps']
    assert bool(top) == (mode in {'top_level', 'both'})
    assert bool(scoped) == (mode in {'project_only', 'both'})
    entries = top + scoped
    assert {v['id'] for v in entries} == {'a'*32}
    assert {v['target'] for v in entries} == {'vps:'+'a'*32}
    # Display association never becomes a mandatory local project execution route.
    result = server.execute('exec', target='vps:'+'a'*32, command='printf whole-vps')
    assert result['output'] == 'whole-vps'
    assert errors == []
