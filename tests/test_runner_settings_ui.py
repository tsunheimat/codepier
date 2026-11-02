"""Real browser owner settings, non-authorized export and native form coverage."""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from shared.runner_settings import OWNER_FIELDS
from tests.test_workspace_browser import workspace_browser, workspace_server  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration]


def open_settings(context, server, width=1440):
    page = context.new_page()
    page.set_viewport_size({'width':width,'height':1000})
    page.goto(server.url+'/#resources/devices')
    page.locator('[data-action="device-detail"][data-id="a"]').click()
    page.locator('#open-runner-settings').click()
    expect(page.locator('[data-rs-group]')).to_have_count(15,timeout=30000)
    return page


@pytest.mark.parametrize('width',[390,1440])
def test_unapproved_owner_can_review_every_group_and_export_but_not_apply(workspace_server,workspace_browser,width,tmp_path):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_settings(context,server,width)
    assert set(page.locator('[data-rs-group]').evaluate_all('(nodes)=>nodes.map(node=>node.dataset.rsGroup)')) == set(OWNER_FIELDS)
    expect(page.locator('[data-rs-apply]')).to_be_disabled()
    page.locator('[data-rs-group="name"] > summary').click()
    page.get_by_role('textbox',name='name',exact=True).fill('Draft owner name')
    expect(page.locator('[data-rs-apply]')).to_be_disabled()
    expect(page.locator('[data-rs-preview]')).to_be_enabled()
    page.locator('[data-rs-preview]').click()
    expect(page.locator('[data-rs-status]')).to_contain_text('验证通过',timeout=30000)
    assert json.loads(server.config_path.read_text())['name']=='Runner a'
    with page.expect_download() as download:
        page.locator('[data-rs-export]').click()
    path=tmp_path/'export.json';download.value.save_as(path)
    assert json.loads(path.read_text())=={'name':'Draft owner name'}
    assert server.config['secret'] not in page.locator('[data-runner-settings]').inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert errors==[]


def test_ssh_discovery_shows_owner_approval_without_testing_connection(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    page=open_settings(context,server)
    page.locator('[data-rs-discover]').click()
    expect(page.locator('[data-rs-hosts]')).to_contain_text('fixture-extra',timeout=30000)
    expect(page.locator('[data-rs-probe]')).to_have_count(2)
    expect(page.locator('[data-rs-hosts]')).to_contain_text('configured')
    page.locator('[data-rs-group="ssh"] > summary').click()
    expect(page.get_by_role('textbox',name='ssh.targets.fixture.host_alias',exact=True)).to_have_value('fixture')
    expect(page.get_by_role('textbox',name='ssh.targets.fixture.config_file',exact=True)).to_have_value(str(server.ssh_config))
    assert errors==[]


def test_space_selects_parent_vps_inline_without_discovery_or_per_row_dialog(workspace_server,workspace_browser):
    server,(context,errors)=workspace_server,workspace_browser
    resource=next(r for r in server.must(server.client.get('/api/vps'))['vps'] if r['host_alias']=='fixture-extra')
    page=context.new_page();scans=[]
    page.on('request',lambda r:scans.append(r.url) if '/ssh-hosts' in r.url else None)
    page.goto(server.url+'/#spaces/alice/vps')
    row=page.locator('[data-selection-row="'+resource['id']+'"]')
    expect(row).to_be_visible()
    expect(page.locator('#workspace-form')).to_have_count(0)
    row.locator('[data-selection-check]').check()
    row.locator('[data-selection-alias]').fill('Space presentation')
    row.locator('[data-selection-description]').fill('Space-only note')
    expect(row.locator('[name="runner_device_id"],[name="connection_ref"],[name="host_alias"]')).to_have_count(0)
    page.locator('[data-selection-save]').click()
    expect(page.locator('[data-selection-status]')).to_have_text('没有未保存的修改',timeout=15000)
    binding=next(r for r in server.must(server.client.get('/api/spaces/alice/vps'))['vps'] if r['resource_id']==resource['id'])
    assert binding['name']=='Space presentation' and binding['description']=='Space-only note'
    assert not scans and errors==[]
