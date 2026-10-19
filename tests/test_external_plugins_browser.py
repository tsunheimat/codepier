"""Real browser forms against the real Hub/IAM; only external MCP is a fixture."""
import os
import uuid
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.external_plugin_support import plugin_env, setup, invoke, code
from tests.identity_fixtures import set_member, must


@pytest.fixture(params=['chromium', 'webkit'])
def plugin_browser(request):
    with sync_playwright() as pw:
        executable = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = getattr(pw, request.param).launch(**({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()


def open_page(browser, value, user, width):
    context = browser.new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    errors = []
    auth_observations=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("""
        window.EventSource = class extends EventTarget {
            constructor(url) { super(); this.url = new URL(url, location.href).href; this.readyState = 1; }
            close() { this.readyState = 2; }
        };
    """)
    def route(request_route):
        req = request_route.request
        parts = urlsplit(req.url)
        if parts.hostname != '127.0.0.1':
            request_route.abort()
            return
        # The ASGI transport is shared by fixture clients; its own cookie jar
        # must never supply identity to a fresh browser context. Only forward
        # the actual browser Cookie header, including an explicitly empty one.
        browser_headers=req.all_headers()
        # WebKit's intercepted request omits Cookie in this test runtime even
        # after accepting Set-Cookie. Forward only cookies for this URL from
        # this actual browser context, never the shared ASGI client's jar.
        browser_headers['cookie']=req.header_value('cookie') or '; '.join(
            cookie['name']+'='+cookie['value'] for cookie in context.cookies(req.url))
        response = value['b'][user].client.request(req.method, req.url, headers=browser_headers,
            content=req.post_data_buffer, follow_redirects=False)
        if parts.path in {'/api/login','/api/session','/api/iam/me'}:
            observed=response.json()
            auth_observations.append({'path':parts.path,'status':response.status_code,
                'received_cookie':bool(browser_headers['cookie']),
                'sets_cookie':'set-cookie' in response.headers,
                'authenticated':observed.get('authenticated'),
                'error_code':observed.get('error',{}).get('code')})
        headers = {k: v for k, v in response.headers.items() if k not in ('content-length', 'content-encoding')}
        request_route.fulfill(status=response.status_code, headers=headers, body=response.content)
    page.route('**/*', route)
    # Use the real login response/cookie flow in both engines; no preinstalled
    # cross-host cookie or forced principal is allowed to hide auth failures.
    page.goto('http://127.0.0.1:8765/#identity')
    expect(page.locator('#login-form')).to_be_visible()
    page.fill('#username',user);page.fill('#password','isolated-plugin-browser-password')
    with page.expect_response('**/api/login') as login:
        page.click('#login-form button')
    assert login.value.status==200,auth_observations
    try:
        expect(page.locator('#page h1')).to_have_text('账号与身份',timeout=15000)
    except AssertionError:
        # Only booleans/status codes, never cookie values or response bodies.
        print('Isolated auth observations:',auth_observations)
        print('Browser has session cookie:',any(c['name']=='rd_session' for c in context.cookies()))
        context.close()
        raise
    return context, page, errors


def save(page):
    page.click('button[form="ext-form"]')
    expect(page.locator('#ext-form')).to_have_count(0, timeout=15000)


@pytest.mark.parametrize('width', [1280, 390])
def test_plugin_setup_bind_authorize_consent_and_detach(plugin_env, plugin_browser, width, tmp_path):
    value = setup(plugin_env, consent=False)
    owner_context, page, errors = open_page(plugin_browser, value, 'owner', width)
    viewer_context = None
    try:
        page.goto('http://127.0.0.1:8765/#spaces/team/mcp')
        expect(page.locator('#external-plugins-panel')).to_be_visible()
        page.click('[data-ext-action="configure"][data-ext-id=""]')
        page.fill('#ext-form [name="alias"]', 'ui-main')
        page.select_option('#ext-form [name="backend-0"]', value['binding']['id'])
        page.select_option('#ext-form [name="tool-0-0"]', 'backend_pipeline')
        page.select_option('#ext-form [name="tool-0-1"]', 'backend_list')
        page.check('#ext-form [name="reviewed"]')
        save(page)
        row = value['app'].state.store.one("SELECT * FROM external_integrations WHERE alias='ui-main'")
        assert row and row['version'] == 1
        page.click(f'[data-ext-action="projects"][data-ext-id="{row["id"]}"]')
        page.select_option('#ext-form [name="project"]', 'project-a')
        page.fill('#ext-form [name="project-config-0"]', '321')
        save(page)
        page.click(f'[data-ext-action="profile"][data-ext-id="{row["id"]}"]')
        page.select_option('#ext-form [name="profile"]', value['profile']['id'])
        page.select_option('#ext-form [name="target"]', 'project-a')
        page.check('#ext-form [name="action"][value="pipeline.get"]')
        save(page)
        assert code(invoke(value, name='ext.ui-main.pipeline.get')) == 'UNKNOWN_TOOL'
        current=must(value['b']['owner'].get('/api/spaces/team/profiles/'+value['profile']['id']))
        rules=current['policy']['plugin_rules']
        assert [r for r in rules if r['integration_id']!=row['id']]==value['profile']['policy']['plugin_rules']
        added=[r for r in rules if r['integration_id']==row['id']]
        assert len(added)==1 and added[0]['projects']==['project-a'] and added[0]['actions']==['pipeline.get']
        assert not current['policy']['connector_rules']
        # Profile editing alone cannot grant the human new resources. A distinct
        # administrator action supplies the member's matching direct rights.
        set_member(value['b']['owner'],'viewer',current['policy'])
        assert code(invoke(value,name='ext.ui-main.pipeline.get'))=='UNKNOWN_TOOL'
        assert 'FIXTURE_BACKEND_CREDENTIAL' not in page.locator('body').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'plugins-{width}.png'), full_page=True)

        viewer_context, viewer, viewer_errors = open_page(plugin_browser, value, 'viewer', width)
        viewer.goto('http://127.0.0.1:8765/#spaces/team/connections')
        viewer.click(f'[data-ws-action="plugin-consent"][data-id="{value["grant"]["connection_id"]}"]')
        expect(viewer.locator('#ext-form')).to_contain_text('321')
        expect(viewer.locator('#ext-form')).to_contain_text('backend_pipeline')
        viewer.check('#ext-form input[type="checkbox"]')
        save(viewer)
        expect(viewer.locator('#workspace-page')).to_contain_text(row['id'])
        assert invoke(value, name='ext.ui-main.pipeline.get')['structuredContent']['arguments']['project_id'] == 321
        assert '"binding_id"' not in value['app'].state.store.one('SELECT policy FROM access_profiles WHERE id=?',
            (value['profile']['id'],))['policy']

        page.click(f'[data-ext-action="projects"][data-ext-id="{row["id"]}"]')
        page.select_option('#ext-form [name="project"]', 'project-a')
        page.select_option('#ext-form [name="mode"]', 'detach')
        save(page)
        assert code(invoke(value, name='ext.ui-main.pipeline.get')) == 'UNKNOWN_TOOL'
        assert value['app'].state.store.one('SELECT id FROM external_integrations WHERE id=?', (row['id'],))
        assert not errors + viewer_errors, errors + viewer_errors
    finally:
        if viewer_context:
            viewer_context.close()
        owner_context.close()


def test_plugin_form_does_not_submit_after_space_change(plugin_env, plugin_browser):
    value = setup(plugin_env)
    context, page, errors = open_page(plugin_browser, value, 'owner', 1280)
    try:
        page.goto('http://127.0.0.1:8765/#spaces/team/mcp')
        page.click(f'[data-ext-action="toggle"][data-ext-id="{value["integration"]["id"]}"]')
        expect(page.locator('#ext-form')).to_be_visible()
        other=must(value['b']['owner'].post('/api/spaces',json={
            'label':'Other explicit workspace','idempotency_key':uuid.uuid4().hex}),201)
        sent=[]
        page.on('request',lambda request:sent.append(request.url) if request.method=='PATCH' else None)
        page.evaluate('''async (space) => {
          const original=document.querySelector('#ext-form');
          await navigate('spaces/'+space+'/mcp');
          original.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        }''',other['id'])
        expect(page).to_have_url('http://127.0.0.1:8765/#spaces/'+other['id']+'/mcp')
        assert sent==[] and page.evaluate('S.space_id === undefined')
        assert value['app'].state.store.one('SELECT enabled,version FROM external_integrations WHERE id=?',
            (value['integration']['id'],)) == {'enabled': 1, 'version': 1}
        assert not errors
    finally:
        context.close()
