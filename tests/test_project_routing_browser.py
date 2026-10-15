"""Real browser forms against a temporary Hub, built-in IPC and external Agent."""
from __future__ import annotations

import json
import os
import uuid

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.routing_support import routing_stack


@pytest.fixture(params=['chromium', 'webkit'])
def routing_browser(request):
    with sync_playwright() as pw:
        path = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = getattr(pw, request.param).launch(**({'executable_path': path} if path else {}))
        try:
            yield browser
        finally:
            browser.close()


def save(page):
    page.locator('button[form="routing-form"]').click()
    expect(page.locator('#routing-form')).to_have_count(0, timeout=15000)


@pytest.mark.parametrize('width', [1280, 390])
def test_space_source_routes_subfolders_project_override_and_unbind(routing_stack, routing_browser, width, tmp_path):
    r = routing_stack
    page = routing_browser.new_page(viewport={'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(r.s.url + '/#resources/projects')
        page.fill('#username', 'admin')
        page.fill('#password', r.s.password)
        page.click('#login-form button')
        expect(page.locator('#project-routing-panel')).to_be_visible()
        # New shared-source name, entirely separate from the fixture's active source.
        page.locator('[data-routing="source"][data-routing-id=""]').click()
        page.fill('#routing-form [name="name"]', 'browser-shared')
        save(page)
        source = next(s for s in r.get('/api/project-routing')['sources'] if s['name'] == 'browser-shared')
        page.locator(f'[data-routing="mount"][data-routing-id="{source["id"]}"]').click()
        page.select_option('#routing-form [name="device"]', r.builtin)
        page.fill('#routing-form [name="path"]', str(r.s.root))
        page.select_option('#routing-form [name="mode"]', 'write')
        page.check('#routing-form [name="same"]')
        save(page)
        page.locator(f'[data-routing="batch"][data-routing-id="{source["id"]}"]').click()
        page.locator('#routing-browse').click()
        expect(page.locator('#routing-folders')).to_contain_text('Lumen')
        page.locator('#routing-folders label').filter(has_text='Lumen').locator('input').check()
        proposed = json.loads(page.input_value('#routing-form [name="projects"]'))
        assert proposed == [{'alias': 'Lumen', 'subpath': 'Lumen'}]
        proposed[0]['alias'] = 'Browser-Logical'
        page.fill('#routing-form [name="projects"]', json.dumps(proposed))
        save(page)
        new = next(p for p in r.get('/api/projects')['projects'] if p['alias'] == 'Browser-Logical')
        assert new['routing_managed'] and new['source_root_id'] == source['id']
        page.locator(f'[data-routing="metadata"][data-routing-id="{new["id"]}"]').click()
        page.fill('#routing-form [name="description"]', 'metadata-only update')
        save(page)
        page.locator(f'[data-routing="override"][data-routing-id="{new["id"]}"]').click()
        page.select_option('#routing-form [name="family"]', 'git')
        page.select_option('#routing-form [name="route"]', 'disabled')
        save(page)
        detail = r.get('/api/projects/' + new['id'] + '/routing')
        assert next(x for x in detail['project']['execution_routes'] if x['action'] == 'git')['error_code'] == 'PROJECT_ROUTE_DISABLED'
        # The role form edits route permission without adding raw project permissions.
        role = r.post('/api/access-roles', {'label': 'Browser route policy', 'idempotency_key': uuid.uuid4().hex,
            'project_rules': [{'projects': [new['id']], 'actions': ['read']} ]})
        page.locator(f'[data-routing="role"][data-routing-id="{r.routes["files"]["id"]}"]').click()
        page.select_option('#routing-form [name="role"]', role['id'])
        page.check(f'#routing-form [name="project"][value="{new["id"]}"]')
        save(page)
        updated = r.get('/api/access-roles/' + role['id'])
        assert updated['route_rules'] == [{'route_id': r.routes['files']['id'], 'projects': [new['id']], 'all_projects': False}]
        assert updated['project_rules'][0]['actions'] == ['read']
        page.locator(f'[data-routing="binding"][data-routing-id="{new["id"]}"]').click()
        page.select_option('#routing-form [name="source"]', '')
        save(page)
        assert not r.get('/api/projects/' + new['id'] + '/routing')['project']['source_bound']
        assert r.s.lumen.joinpath('README.md').is_file()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'routing-{width}.png'), full_page=True)
        assert not errors, errors
    finally:
        page.close()


def test_stale_space_form_cannot_mutate(routing_stack, routing_browser):
    r = routing_stack
    page = routing_browser.new_page()
    try:
        page.goto(r.s.url + '/#resources/projects')
        page.fill('#username', 'admin'); page.fill('#password', r.s.password); page.click('#login-form button')
        page.locator('[data-routing="source"][data-routing-id=""]').click()
        page.fill('#routing-form [name="name"]', 'must-not-save')
        page.evaluate("S.space_id='different'; document.querySelector('#routing-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));")
        assert not any(s['name'] == 'must-not-save' for s in r.get('/api/project-routing')['sources'])
    finally:
        page.close()
