"""Real-browser inline selection, future VPS consent and no lower-layer SSH scans."""
from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import expect

from tests.test_workspace_browser import workspace_browser, workspace_server, open_workspace, held_account_response  # noqa: F401

pytestmark=[pytest.mark.browser,pytest.mark.integration]


def test_profile_visual_all_future_vps_survives_save_and_includes_later_space_selection(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_workspace(context,server,'spaces/alice/profiles')
    page.locator('[data-ws-action="new-profiles"]').click()
    form=page.locator('#workspace-form')
    form.locator('[name="label"]').fill('Future VPS Shell')
    form.locator('[name="all_vps"]').check()
    expect(form.locator('[data-policy-selected-vps]')).to_be_hidden()
    expect(form.locator('[data-kind="vps"][data-action]')).to_have_count(0)
    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/api/spaces/alice/profiles')) as response:
        page.get_by_role('button',name='保存',exact=True).click()
    assert response.value.ok,response.value.text()
    saved=response.value.json()
    assert saved['policy']['vps_rules']==[{'vps':[],'all_vps':True}]
    expect(form).to_have_count(0)
    page.locator('[data-ws-action="profile"][data-id="'+saved['id']+'"]').click()
    expect(form.locator('[name="all_vps"]')).to_be_checked()
    page.get_by_role('button',name='取消',exact=True).click()
    connection=server.must(server.client.post('/api/spaces/alice/connections',json={
        'name':'All future VPS','profile_id':saved['id'],'profile_version':saved['version'],'idempotency_key':uuid.uuid4().hex}))
    resource=next(r for r in server.must(server.client.get('/api/vps'))['vps'] if r['host_alias']=='fixture-extra')
    scans=[];page.on('request',lambda request:scans.append(request.url) if '/ssh-hosts' in request.url else None)
    page.evaluate('navigate("spaces/alice/vps")')
    row=page.locator('[data-selection-row="'+resource['id']+'"]')
    row.locator('[data-selection-check]').check()
    row.locator('[data-selection-alias]').fill('Added after Connection')
    page.locator('[data-selection-save]').click()
    expect(page.locator('[data-selection-status]')).to_have_text('没有未保存的修改',timeout=15000)
    visible=server.mcp('vps',token=connection['token'])['vps']
    assert any(r['name']=='Added after Connection' and r['shell_access'] for r in visible)
    assert scans==[] and errors==[]


@pytest.mark.parametrize('width',[390,1440])
def test_device_inline_selection_alias_and_responsive_rows(workspace_server,workspace_browser,width):
    server,(context,errors)=workspace_server,workspace_browser
    page=context.new_page();page.set_viewport_size({'width':width,'height':1000})
    page.goto(server.url+'/#spaces/alice/devices')
    row=page.locator('[data-selection-row="a"]')
    row.locator('[data-selection-check]').check()
    row.locator('[data-selection-alias]').fill('My work Runner')
    page.locator('[data-selection-row="b"] [data-selection-check]').uncheck()
    page.locator('[data-selection-save]').click()
    expect(page.locator('[data-selection-status]')).to_have_text('没有未保存的修改',timeout=15000)
    rows=server.must(server.client.get('/api/spaces/alice/devices'))['devices']
    assert 'b' not in {r['id'] for r in rows}
    assert next(r for r in rows if r['id']=='a')['name']=='My work Runner'
    expect(page.locator('#workspace-form')).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert errors==[]


def test_passive_render_preserves_selection_draft_and_navigation_can_be_cancelled(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_workspace(context,server,'spaces/alice/vps')
    alias=page.locator('[data-selection-row="server"] [data-selection-alias]')
    alias.fill('Unsaved alias')
    page.evaluate('renderPage(false)')
    expect(alias).to_have_value('Unsaved alias')
    expect(page.locator('[data-selection-save]')).to_be_enabled()
    page.once('dialog',lambda dialog:dialog.dismiss())
    page.evaluate('navigate("spaces/alice/devices")')
    expect(page).to_have_url(re.compile('#spaces/alice/vps$'))
    expect(alias).to_have_value('Unsaved alias')
    page.once('dialog',lambda dialog:dialog.accept())
    page.evaluate('navigate("spaces/alice/devices")')
    expect(page.locator('[data-workspace-selection="devices"]')).to_be_visible()
    page.evaluate('navigate("spaces/alice/vps")')
    expect(alias).to_have_value('Test server')
    assert errors==[]


def test_uncertain_batch_save_is_not_replayed_and_explicit_reload_recovers(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_workspace(context,server,'spaces/alice/vps');writes=[]
    endpoint=server.url+'/api/spaces/alice/vps-selection'
    def drop_write_response(route):
        if route.request.method!='PUT':return route.continue_()
        writes.append(route.request.post_data_json)
        response=route.fetch()
        assert response.status==200
        route.abort('failed')
    page.route(endpoint,drop_write_response)
    page.locator('[data-selection-row="server"] [data-selection-alias]').fill('Saved response lost')
    page.locator('[data-selection-save]').click()
    expect(page.locator('[data-selection-status]')).to_contain_text('不确定',timeout=15000)
    expect(page.locator('[data-selection-save]')).to_be_disabled()
    assert len(writes)==1
    # Passive refresh and same-account reauthorization retain only the
    # unresolved-save marker, not permission to replay the PUT or old drafts.
    page.evaluate('renderPage(false)')
    expect(page.locator('[data-selection-status]')).to_contain_text('不确定')
    page.evaluate('recoverPanelAccess()')
    expect(page.locator('[data-selection-status]')).to_contain_text('不确定')
    expect(page.locator('[data-selection-save]')).to_be_disabled()
    assert len(writes)==1
    page.unroute(endpoint,drop_write_response)
    page.once('dialog',lambda dialog:dialog.accept())
    page.locator('[data-selection-reload]').click()
    expect(page.locator('[data-selection-row="server"] [data-selection-alias]')).to_have_value('Saved response lost')
    expect(page.locator('[data-selection-status]')).to_have_text('没有未保存的修改')
    assert len(writes)==1 and errors==[]


def test_late_parent_list_response_cannot_replace_other_workspace_tab(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_workspace(context,server,'spaces/alice/vps')
    with held_account_response(page,'/api/spaces/alice/vps-selection'):
        page.evaluate('void renderPage(false)')
        expect(page.locator('html')).to_have_attribute('data-test-account-response-status','200',timeout=15000)
        page.evaluate('navigate("spaces/alice/devices")')
        expect(page.locator('[data-workspace-selection="devices"]')).to_be_visible()
        page.evaluate('accountResponseGate.release()')
    expect(page).to_have_url(re.compile('#spaces/alice/devices$'))
    expect(page.locator('[data-workspace-selection="vps"]')).to_have_count(0)
    assert errors==[]


def test_parent_runner_inventory_event_updates_open_space_without_ssh_refresh(workspace_server,workspace_browser):
    import json
    from shared.util import atomic_json
    server,(context,errors)=workspace_server,workspace_browser
    page=open_workspace(context,server,'spaces/alice/vps')
    scans=[];page.on('request',lambda request:scans.append(request.url) if '/ssh-hosts' in request.url else None)
    config=json.loads(server.config_path.read_text())
    config['ssh']['targets']['fresh_event']={'host_alias':'fresh-event','config_file':str(server.ssh_config),
        'shell':'sh','host_key_policy':'strict'}
    atomic_json(server.config_path,config)
    expect(page.locator('[data-selection-row]').filter(has_text='fresh-event')).to_be_visible(timeout=30000)
    assert scans==[] and errors==[]
