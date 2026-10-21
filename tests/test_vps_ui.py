"""Real VPS inventory/workspace forms on Chromium and WebKit, no SSH secrets.

The three discovery modes are also exercised in test_workspace_browser. These
cases retain responsive editing, escaping, stale-save and real-command checks.
"""
import re
import shlex
import uuid

import pytest
from playwright.sync_api import expect

from tests.test_workspace_browser import workspace_browser, workspace_server, open_workspace  # noqa: F401
from tests.test_vps import input_body

pytestmark = [pytest.mark.browser, pytest.mark.integration]


def open_inventory(context, server, width=1440):
    page = context.new_page()
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(server.url+'/#resources/vps')
    expect(page.locator('#workspace-inventory')).to_be_visible(timeout=15000)
    return page


@pytest.mark.parametrize('width', [390, 1440])
def test_inventory_create_edit_escapes_text_and_fits_viewport(workspace_server, workspace_browser, width):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_inventory(context, server, width)
    page.locator('[data-ws-action="inventory-new"]').click()
    form = page.locator('#workspace-form')
    name = '<img src=x onerror=window.injected=true>'
    form.locator('[name="name"]').fill(name)
    form.locator('[name="runner_device_id"]').select_option('a')
    form.locator('[name="host_alias"]').fill('fixture-extra')
    form.locator('[name="description"]').fill('Display metadata only')
    expect(form.locator('[name="password"], [name="private_key"]')).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    expect(page.locator('#workspace-inventory h3').filter(has_text=name)).to_have_count(1)
    assert page.evaluate('window.injected === undefined')
    expect(page.locator('#workspace-inventory img')).to_have_count(0)
    saved = next(v for v in server.must(server.client.get('/api/vps'))['vps'] if v['name']==name)
    page.locator('[data-ws-action="inventory-edit"][data-id="'+saved['id']+'"]').click()
    form.locator('[name="description"]').fill('Edited metadata')
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    current = next(v for v in server.must(server.client.get('/api/vps'))['vps'] if v['id']==saved['id'])
    assert current['version'] == saved['version']+1 and current['description'] == 'Edited metadata'
    assert errors == []


def test_inventory_stale_save_keeps_draft_and_does_not_overwrite(workspace_server, workspace_browser):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_inventory(context, server)
    original = server.must(server.client.get('/api/vps'))['vps'][0]
    page.locator('[data-ws-action="inventory-edit"][data-id="'+original['id']+'"]').click()
    form = page.locator('#workspace-form')
    form.locator('[name="description"]').fill('Local unsaved draft')
    server.finish(server.must(server.client.get('/api/devices/a/ssh-hosts')))
    server.must(server.client.put('/api/vps/'+original['id'], json=input_body(
        name=original['name'], description='Saved by another view', expected_version=original['version'])))
    with page.expect_response(lambda response: response.request.method == 'PUT' and
                              response.url.endswith('/api/vps/'+original['id'])) as save:
        page.get_by_role('button', name='保存', exact=True).click()
    assert save.value.status == 409
    expect(form.locator('[data-ws-status]')).to_have_text(save.value.json()['error']['message'], timeout=15000)
    expect(form).to_be_visible()
    expect(form.locator('[name="description"]')).to_have_value('Local unsaved draft')
    current = next(v for v in server.must(server.client.get('/api/vps'))['vps'] if v['id']==original['id'])
    assert current['description'] == 'Saved by another view'
    assert errors == []


def test_workspace_binding_identity_and_project_display_are_explicit(workspace_server, workspace_browser):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_workspace(context, server, 'spaces/alice/vps')
    page.locator('[data-ws-action="vps"][data-id="'+'a'*32+'"]').click()
    form = page.locator('#workspace-form')
    expect(form.locator('[name="resource_id"], [name="runner_device_id"], [name="connection_ref"]')).to_have_count(0)
    expect(form.locator('[data-vps-projects]')).to_be_hidden()
    form.locator('[name="display_mode"]').select_option('both')
    expect(form.locator('[data-vps-projects]')).to_be_visible()
    form.locator('[name="projects"][value="project"]').check()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form).to_have_count(0, timeout=15000)
    top = server.mcp('vps')['vps']
    scoped = server.mcp('vps', {'project': 'codepier'})['vps']
    assert top[0]['target'] == scoped[0]['target'] == 'vps:'+'a'*32
    assert errors == []


def test_ssh_command_form_runs_original_target_once_without_local_project(workspace_server, workspace_browser):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_workspace(context, server, 'spaces/alice/vps')
    marker = server.directory/('ui-once-'+uuid.uuid4().hex)
    page.locator('[data-ws-action="vps-command"][data-id="'+'a'*32+'"]').click()
    form = page.locator('#workspace-form')
    expect(form.locator('[name="project"]')).to_have_count(0)
    form.locator('[name="command"]').fill('printf once >> '+shlex.quote(str(marker))+'; printf ui-ssh-result')
    page.get_by_role('button', name='保存', exact=True).click()
    expect(page.locator('#modal-title')).to_have_text('SSH 结果', timeout=30000)
    expect(page.locator('.modal-body')).to_contain_text('ui-ssh-result')
    assert marker.read_text() == 'once'
    assert re.search('password|private_key', page.locator('.modal-body').inner_text()) is None
    assert errors == []


def test_background_refresh_does_not_steal_held_edit_click(workspace_server, workspace_browser):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_inventory(context, server, 390)
    page.evaluate('''() => {
      document.addEventListener('pointerdown', event => {
        const button = event.target.closest('[data-ws-action="inventory-edit"]');
        if (!button) return;
        window.heldVpsButton = button;
        window.detachedWhileHeld = false;
        window.vpsRefreshObserver = new MutationObserver(() => {
          if (uiPagePointers.size && !button.isConnected) window.detachedWhileHeld = true;
        });
        vpsRefreshObserver.observe(document.querySelector('#page'), {childList:true, subtree:true});
        void renderPage(false).then(() => { document.body.dataset.vpsRefreshFinished='true'; });
      }, {once:true, capture:true});
    }''')
    button = page.locator('[data-ws-action="inventory-edit"]').first
    identifier = button.get_attribute('data-id')
    button.click(delay=250)
    expect(page.locator('#workspace-form')).to_be_visible(timeout=15000)
    expect(page.locator('body')).to_have_attribute('data-vps-refresh-finished', 'true')
    assert page.evaluate('!window.detachedWhileHeld')
    assert page.evaluate('window.heldVpsButton !== undefined')
    value = next(v for v in server.must(server.client.get('/api/vps'))['vps'] if v['id']==identifier)
    expect(page.locator('#workspace-form [name="name"]')).to_have_value(value['name'])
    page.evaluate('vpsRefreshObserver.disconnect()')
    assert errors == []


def test_explicit_workspace_navigation_does_not_reopen_an_inventory_draft(workspace_server, workspace_browser):
    server, (context, errors) = workspace_server, workspace_browser
    page = open_inventory(context, server)
    page.locator('[data-ws-action="inventory-edit"]').first.click()
    form = page.locator('#workspace-form')
    form.locator('[name="description"]').fill('In-memory unsaved draft')
    prompts=[]
    def keep_draft(dialog):
        prompts.append(dialog.message)
        dialog.dismiss()
    page.once('dialog',keep_draft)
    page.evaluate('navigate("spaces/alice/vps")')
    assert len(prompts)==1 and '未保存' in prompts[0]
    expect(form).to_be_visible()
    expect(form.locator('[name="description"]')).to_have_value('In-memory unsaved draft')
    expect(page).to_have_url(re.compile(r'#resources/vps$'))
    page.once('dialog',lambda dialog:dialog.accept())
    page.evaluate('navigate("spaces/alice/vps")')
    expect(page).to_have_url(re.compile(r'#spaces/alice/vps$'), timeout=15000)
    expect(form).to_have_count(0)
    expect(page.locator('#workspace-page h2')).to_have_text('VPS')
    assert all(v['description'] != 'In-memory unsaved draft' for v in server.must(server.client.get('/api/vps'))['vps'])
    assert errors == []
