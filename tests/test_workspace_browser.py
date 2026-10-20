"""Real browser acceptance for explicit workspaces, not DOM/template simulations.

Every test uses a disposable Hub/Runner and synthetic session cookies. No live
workspace, browser profile, SSH host, or user credential is accessed.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
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


@contextmanager
def held_account_response(page, lookup, *, initial=False):
    """Hold one authentic response without intercepting unrelated HTTP or SSE."""
    script='''lookup => {
        const original = window.fetch;
        const gate = window.accountResponseGate = { original, status: null, count: 0 };
        const released = new Promise(resolve => { gate.release = resolve; });
        window.fetch = async function(...args) {
            const response = await original.apply(this, args);
            if (args[0] === lookup && gate.count === 0) {
                gate.count++;
                gate.status = response.status;
                window.fetch = original;
                await released;
            }
            return response;
        };
    }'''
    if initial:
        page.add_init_script('('+script+')('+json.dumps(lookup)+');')
    else:
        page.evaluate(script,lookup)
    try:
        yield
    finally:
        if not page.is_closed():
            page.evaluate('''() => {
                const gate = window.accountResponseGate;
                if (!gate) return;
                window.fetch = gate.original;
                gate.release();
                delete window.accountResponseGate;
            }''')


@pytest.mark.parametrize('lookup',['/api/session','/api/iam/me','/api/devices','/api/projects'])
def test_initial_account_lookup_survives_workspace_navigation_without_replaying_writes(workspace_server,workspace_browser,lookup):
    server=workspace_server;context,errors=workspace_browser
    writes=[];page=context.new_page()
    page.on('request',lambda request:writes.append(request.url)
        if request.method not in {'GET','HEAD','OPTIONS'} else None)
    with held_account_response(page,lookup,initial=True):
        # The workspace listing intentionally does not enumerate global
        # devices/projects. Exercise those account snapshots from their actual
        # resource-page entrypoint, not by requiring unused startup requests.
        start='resources/devices' if lookup in {'/api/devices','/api/projects'} else 'spaces'
        with page.expect_request(server.url+lookup):
            page.goto(server.url+'/#'+start,wait_until='domcontentloaded')
        page.wait_for_function('accountResponseGate.status !== null')
        assert page.evaluate('accountResponseGate.status')==200
        assert page.evaluate('accountResponseGate.count')==1
        # The server response is real; only its delivery to the application is
        # delayed until the explicit view changes. Do not toggle interception
        # that can also pause the next, unrelated account request.
        page.evaluate('location.hash="#spaces/alice/profiles"')
        page.evaluate('accountResponseGate.release()')
        try:
            expect(page.locator('#workspace-page h2')).to_have_text('Profiles',timeout=15000)
        except AssertionError:
            # Bounded diagnostics from a disposable fixture only, no cookies,
            # token values, draft contents or user resource data.
            print('Boot navigation state',page.evaluate('''()=>({
                user:S.session?.user_id,page:S.page,identity:!!S.identity,
                hash:location.hash,renderSeq:S.renderSeq,
                accountReads:performance.getEntriesByType('resource')
                    .filter(x=>x.name.includes('/api/')).map(x=>new URL(x.name).pathname)
            })'''),errors)
            raise
        expect(page).to_have_url(re.compile(r'#spaces/alice/profiles$'))
        expect(page.locator('#retry-boot')).to_have_count(0)
        assert page.evaluate('S.session.user_id')=='alice'
        assert writes==[] and errors==[]


def test_project_search_counts_actual_scope_and_keeps_only_same_page_drafts(workspace_server,workspace_browser):
    server=workspace_server;context,errors=workspace_browser
    page=open_workspace(context,server,'spaces/alice/projects')
    count=len(server.must(server.client.get('/api/spaces/alice/projects'))['projects'])
    expect(page.locator('#project-count')).to_have_text(f'{count} / {count} 个项目')
    search=page.locator('#project-query')
    search.fill('no-matching-source')
    expect(page.locator('#project-no-match')).to_be_visible()
    expect(page.locator('#project-count')).to_have_text(f'0 / {count} 个项目')
    # A passive render cannot overwrite a search draft on this same page.
    page.evaluate('renderPage(false)')
    expect(search).to_have_value('no-matching-source')
    expect(page.locator('[data-project-row]:visible')).to_have_count(0)
    page.get_by_role('button',name='清除筛选',exact=True).click()
    expect(search).to_have_value('');expect(search).to_be_focused()
    expect(page.locator('[data-project-row]:visible')).to_have_count(count)
    search.fill('codepier')
    page.evaluate('navigate("resources/projects")')
    expect(page.locator('#resources-page')).to_be_visible()
    expect(search).to_have_value('')
    count=len(server.must(server.client.get('/api/spaces/alice/projects'))['projects'])
    expect(page.locator('#project-count')).to_have_text(f'{count} / {count} 个项目')
    page.locator('[data-project-detail="project"]').click()
    expect(page.locator('#modal-title')).to_contain_text('codepier')
    assert 'space_id' not in page.evaluate('Object.keys(S)')
    assert errors==[]


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
    created_url = second.url
    second.go_back()
    expect(second).to_have_url(re.compile(r'#spaces$'))
    expect(second.get_by_role('button', name='新建工作空间', exact=True)).to_be_visible()
    second.go_forward()
    expect(second).to_have_url(created_url)
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


def test_navigation_during_revalidation_preserves_url_without_old_content(workspace_server,workspace_browser):
    server=workspace_server;context,errors=workspace_browser
    page=open_workspace(context,server,'spaces/alice/projects');reads=[];writes=[]
    assert page.evaluate('!S.work.project && !ChatUI.project && ChatUI.views.size===0')
    page.on('request',lambda request:(reads if request.method=='GET' else writes).append(urlparse(request.url).path))
    url=server.url+'/api/iam/me'
    # Use the same response-only barrier as cold boot. Real cookies, response
    # bytes, workspace reads and the reconnecting EventSource remain untouched.
    with held_account_response(page,'/api/iam/me'):
        with page.expect_request(url):page.evaluate('void recoverPanelAccess()')
        page.wait_for_function('accountResponseGate.status !== null')
        assert page.evaluate('accountResponseGate.status')==200
        assert page.evaluate('accountResponseGate.count')==1
        expect(page.locator('#breadcrumb-page')).to_have_count(0)
        page.evaluate('navigate("spaces/alice/folders")')
        expect(page).to_have_url(server.url+'/#spaces/alice/folders')
        expect(page.locator('#workspace-page')).to_have_count(0)
        page.evaluate('accountResponseGate.release()')
        expect(page.locator('#workspace-page h2')).to_have_text('Folders',timeout=15000)
        assert '/api/spaces/alice/folders' in reads
        assert '/api/devices' not in reads and '/api/projects' not in reads
        assert page.evaluate('Object.hasOwn(S,"space_id")') is False
        assert writes==[] and errors==[]
