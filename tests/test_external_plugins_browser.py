"""Real browser forms against the real Hub/IAM; only external MCP is a fixture."""
import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.external_plugin_support import plugin_env, setup, invoke, code


@pytest.fixture(params=['chromium', 'webkit'])
def plugin_browser(request):
    with sync_playwright() as pw:
        executable = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = getattr(pw, request.param).launch(**({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()


def open_page(browser, value, user, width):
    context = browser.new_context(viewport={'width': width, 'height': 900})
    context.add_cookies([{'name': 'rd_session', 'value': value['b'][user].cookie, 'domain': '127.0.0.1', 'path': '/'}])
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("sessionStorage.setItem('codepier-space:" + user + "','team');" + """
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
        response = value['b'][user].client.request(req.method, req.url, headers=req.all_headers(),
            content=req.post_data_buffer, follow_redirects=False)
        headers = {k: v for k, v in response.headers.items() if k not in ('content-length', 'content-encoding')}
        request_route.fulfill(status=response.status_code, headers=headers, body=response.content)
    page.route('**/*', route)
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
        page.goto('http://127.0.0.1:8765/#resources/mcp')
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
        page.click(f'[data-ext-action="role"][data-ext-id="{row["id"]}"]')
        page.select_option('#ext-form [name="role"]', value['role']['id'])
        page.select_option('#ext-form [name="target"]', 'project-a')
        page.check('#ext-form [name="action"][value="pipeline.get"]')
        save(page)
        assert code(invoke(value, name='ext.ui-main.pipeline.get')) == 'UNKNOWN_TOOL'
        assert 'FIXTURE_BACKEND_CREDENTIAL' not in page.locator('body').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'plugins-{width}.png'), full_page=True)

        viewer_context, viewer, viewer_errors = open_page(plugin_browser, value, 'viewer', width)
        viewer.goto('http://127.0.0.1:8765/#access/connections')
        viewer.click(f'[data-ext-action="consent"][data-ext-id="{value["grant"]["grant_id"]}"]')
        expect(viewer.locator('#ext-form')).to_contain_text('321')
        expect(viewer.locator('#ext-form')).to_contain_text('backend_pipeline')
        viewer.check('#ext-form input[type="checkbox"]')
        save(viewer)
        expect(viewer.locator('.connection-permissions')).to_contain_text('Plugin ui-main')
        assert invoke(value, name='ext.ui-main.pipeline.get')['structuredContent']['arguments']['project_id'] == 321
        assert not value['app'].state.store.one('SELECT policy FROM access_roles WHERE id=?',
            (value['role']['id'],))['policy'].count('"binding_id"')

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
        page.goto('http://127.0.0.1:8765/#resources/mcp')
        page.click(f'[data-ext-action="toggle"][data-ext-id="{value["integration"]["id"]}"]')
        expect(page.locator('#ext-form')).to_be_visible()
        page.evaluate("S.space_id = 'changed-space'; document.querySelector('#ext-form').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))")
        assert value['app'].state.store.one('SELECT enabled,version FROM external_integrations WHERE id=?',
            (value['integration']['id'],)) == {'enabled': 1, 'version': 1}
        assert not errors
    finally:
        context.close()
