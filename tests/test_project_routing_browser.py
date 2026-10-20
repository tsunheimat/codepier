"""Explicit workspace storage/Folder forms with real built-in IPC and Runner.

Removed SourceRoot/ActionRoute/Role pages are not emulated. These cases verify
current mappings, source-bounded project configuration and non-destructive cleanup.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from tests.routing_support import routing_stack  # noqa: F401


@pytest.fixture(params=['chromium','webkit'])
def routing_browser(request,chat_browser_pool):
    return chat_browser_pool(request.param)


def save(page,fixture,path,method='POST'):
    form=page.locator('#workspace-form')
    with page.expect_response(lambda response: response.request.method==method and
                              response.url.endswith(path)) as response:
        page.locator('button[form="workspace-form"]').click()
    first=response.value
    if first.status==409:
        error=first.json()['error'];assert error['code']=='VALIDATION_PENDING',error
        key=first.request.post_data_json['idempotency_key']
        result=fixture.s.poll(error['operation_id'],timeout=45)
        assert result['state']=='succeeded',result
        # Explicitly continue the unchanged form and its original request key,
        # never manufacture a new validation after an ambiguous first request.
        with page.expect_response(lambda response:response.request.method==method and
                                  response.url.endswith(path)) as response:
            page.locator('button[form="workspace-form"]').click()
        assert response.value.request.post_data_json['idempotency_key']==key
        first=response.value
    assert first.ok,first.text()
    value=first.json()
    expect(form).to_have_count(0,timeout=15000)
    return value


def navigate(page,fixture,tab):
    page.evaluate('(route)=>navigate(route)','spaces/'+fixture.s.space_id+'/'+tab)
    expect(page.locator('#workspace-page')).to_be_visible(timeout=15000)
    expect(page.locator('#workspace-page h2')).to_have_text({
        'sources':'Folder / Storage Pool','projects':'Projects'}[tab])


@pytest.mark.parametrize('width',[1280,390])
def test_shared_storage_folder_project_override_and_cleanup(routing_stack,routing_browser,width,tmp_path):
    r=routing_stack;page=routing_browser.new_page(viewport={'width':width,'height':900})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(r.s.url+'/#spaces/'+r.s.space_id+'/sources')
        page.fill('#username','admin');page.fill('#password',r.s.password);page.click('#login-form button')
        expect(page.locator('#workspace-page h2')).to_have_text('Folder / Storage Pool',timeout=15000)
        expect(page.locator('#project-routing-panel,#routing-form,#role-form')).to_have_count(0)
        page.locator('[data-ws-action="source"][data-id="'+r.folder['id']+'"]').click()
        form=page.locator('#workspace-form')
        expect(form.locator('[name="mapping_ids"]:checked')).to_have_count(2)
        choices=form.locator('[name="default_device_id"] option').evaluate_all(
            'nodes=>nodes.map(n=>n.value).filter(Boolean)')
        assert choices==[r.s.device]  # Hub has files, not implicit Shell authority.
        expect(form.locator('[name="files_mode"]')).to_have_value('inherit')
        expect(form.locator('[name="shell_mode"]')).to_have_value('inherit')
        expect(form.locator('[name="allowed_device_ids"]:checked')).to_have_count(0)
        updated=save(page,r,r.space_path+'/sources/'+r.folder['id'],'PUT')
        assert updated['default_device_id']==r.s.device and updated['files_device_id']==r.builtin
        assert updated['shell']=={'mode':'inherit','default_device_id':None,'allowed_device_ids':[]}

        navigate(page,r,'sources');page.locator('[data-ws-action="new-sources"]').click()
        form.locator('[name="name"]').fill('Browser source')
        form.locator('[name="source_kind"]').select_option('storage')
        form.locator('[name="pool_id"]').select_option(r.pool['id'])
        expect(form.locator('[name="mapping_ids"]:checked')).to_have_count(2)
        form.locator('[name="subpath"]').fill('.')
        form.locator('[name="mode"]').select_option('write')
        form.locator('[name="allow_tasks"]').check()
        folder=save(page,r,r.space_path+'/sources')
        assert folder['source_kind']=='storage' and folder['pool_id']==r.pool['id']

        navigate(page,r,'projects');page.locator('[data-ws-action="new-projects"]').click()
        form.locator('[name="alias"]').fill('Browser-Logical')
        form.locator('[name="source_id"]').select_option(folder['id'])
        form.locator('[name="subpath"]').fill('Lumen')
        expect(form.locator('[name="device_id"],[name="root"],[name="role_id"]')).to_have_count(0)
        project=save(page,r,r.space_path+'/projects')
        assert project['source_id']==folder['id'] and project['subpath']=='Lumen'
        assert 'routing_managed' not in project and 'source_root_id' not in project
        page.locator('[data-project-detail="'+project['id']+'"]').click()
        page.locator('[data-ws-project-shell]').click()
        form.locator('[name="mode"]').select_option('custom')
        form.locator('[name="default_device_id"]').select_option(r.s.device)
        settings=save(page,r,'/api/projects/'+project['id']+'/shell','PUT')
        assert settings['settings']['default_device_id']==r.s.device
        page.locator('[data-project-detail="'+project['id']+'"]').click()
        page.locator('[data-ws-project-shell]').click()
        form.locator('[name="mode"]').select_option('disabled')
        settings=save(page,r,'/api/projects/'+project['id']+'/shell','PUT')
        assert settings['settings']['mode']=='disabled'
        assert r.get('/api/projects/'+project['id']+'/shell')['settings']['mode']=='disabled'
        # Referenced resources cannot be removed underneath a live Project.
        denied=r.s.client.delete(r.space_path+'/sources/'+folder['id'],params={'expected_version':folder['version']})
        assert denied.status_code==409,denied.text
        current=r.get('/api/projects/'+project['id'])
        r.delete('/api/projects/'+project['id']+'?expected_version='+str(current['version']))
        r.delete(r.space_path+'/sources/'+folder['id']+'?expected_version='+str(folder['version']))
        assert (r.s.lumen/'README.md').is_file()
        assert r.get('/api/storage-pools/'+r.pool['id']+'/mappings')['mappings']
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'workspace-routing-{width}.png'),full_page=True)
        assert errors==[]
    finally:page.context.close()


def test_detached_workspace_form_cannot_write_to_either_workspace(routing_stack,routing_browser):
    r=routing_stack;page=routing_browser.new_page();posts=[]
    other=r.post('/api/spaces',{'label':'Different workspace','idempotency_key':uuid.uuid4().hex})
    try:
        page.goto(r.s.url+'/#spaces/'+r.s.space_id+'/sources')
        page.fill('#username','admin');page.fill('#password',r.s.password);page.click('#login-form button')
        page.locator('[data-ws-action="new-sources"]').click()
        page.fill('#workspace-form [name="name"]','must-not-save')
        page.select_option('#workspace-form [name="folder_resource_id"]',r.s.folder_resource['id'])
        page.on('request',lambda request:posts.append(request.url) if request.method=='POST' else None)
        page.on('dialog',lambda dialog:dialog.accept())
        page.evaluate('''route=>{
          const old=document.querySelector('#workspace-form');
          location.hash='#'+route;
          old.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        }''','spaces/'+other['id']+'/sources')
        expect(page).to_have_url(r.s.url+'/#spaces/'+other['id']+'/sources')
        expect(page.locator('#workspace-page h1')).to_contain_text('Different workspace')
        assert not posts
        for space in [r.s.space_id,other['id']]:
            assert not any(folder['name']=='must-not-save' for folder in r.get('/api/spaces/'+space+'/sources')['sources'])
        assert page.evaluate('Object.hasOwn(S,"space_id")') is False
    finally:page.context.close()


def test_pool_inventory_defaults_and_source_import_without_device_grants(routing_stack,routing_browser):
    r=routing_stack;page=routing_browser.new_page()
    assert not r.get(r.space_path+'/devices')['devices']
    try:
        page.goto(r.s.url+'/#resources/storage')
        page.fill('#username','admin');page.fill('#password',r.s.password);page.click('#login-form button')
        page.locator('[data-ws-action="inventory-new"]').click()
        form=page.locator('#workspace-form')
        form.locator('[name="name"]').fill('Pool from browser')
        form.locator('[name="abstract_base"]').fill('/storage/browser-pool')
        for device in (r.builtin,r.s.device):
            form.locator('[name="mapping_device"][value="'+device+'"]').check()
            form.locator('[name="mapping_path_'+device+'"]').fill(str(r.s.root))
        form.locator('[name="default_fs_device_id"]').select_option(r.builtin)
        form.locator('[name="default_shell_device_id"]').select_option(r.s.device)
        form.locator('[name="confirmed_same_storage"]').check()
        pool=save(page,r,'/api/storage-pools')
        assert pool['default_fs_device_id']==r.builtin and pool['default_shell_device_id']==r.s.device
        navigate(page,r,'sources')
        page.locator('[data-ws-action="new-sources"]').click()
        form.locator('[name="name"]').fill('Direct Pool import')
        form.locator('[name="source_kind"]').select_option('storage')
        form.locator('[name="pool_id"]').select_option(pool['id'])
        expect(form.locator('[name="mapping_ids"]:checked')).to_have_count(2)
        form.locator('[name="mode"]').select_option('write')
        form.locator('[name="allow_tasks"]').check()
        source=save(page,r,r.space_path+'/sources')
        assert source['files']['mode']==source['shell']['mode']=='inherit'
        assert source['files_device_id']==r.builtin and source['default_device_id']==r.s.device
        assert not r.get(r.space_path+'/devices')['devices']
        assert source['folder_resource_id'] is None
    finally:
        page.context.close()


def test_folder_inventory_and_direct_source_remain_independent_of_devices(routing_stack,routing_browser):
    r=routing_stack;page=routing_browser.new_page()
    try:
        page.goto(r.s.url+'/#resources/folders')
        page.fill('#username','admin');page.fill('#password',r.s.password);page.click('#login-form button')
        page.locator('[data-ws-action="inventory-new"]').click()
        form=page.locator('#workspace-form')
        form.locator('[name="name"]').fill('Single device folder')
        form.locator('[name="device_id"]').select_option(r.s.device)
        form.locator('[name="device_path"]').fill(str(r.s.root))
        form.locator('[name="mode"]').select_option('write')
        form.locator('[name="allow_tasks"]').check()
        folder=save(page,r,'/api/folders')
        navigate(page,r,'sources')
        page.locator('[data-ws-action="new-sources"]').click()
        form.locator('[name="name"]').fill('Single device import')
        form.locator('[name="folder_resource_id"]').select_option(folder['id'])
        form.locator('[name="subpath"]').fill('Lumen')
        form.locator('[name="mode"]').select_option('write')
        form.locator('[name="allow_tasks"]').check()
        source=save(page,r,r.space_path+'/sources')
        assert source['folder_resource_id']==folder['id'] and source['pool_id'] is None
        assert {v['device_id'] for v in source['views']}=={r.s.device}
        assert source['views'][0]['root']==str(r.s.lumen)
        assert not r.get(r.space_path+'/devices')['devices']
    finally:
        page.context.close()


def test_source_can_be_deactivated_from_list_with_runner_offline(routing_stack,routing_browser):
    r=routing_stack;page=routing_browser.new_page()
    try:
        r.s.stop_agent()
        page.goto(r.s.url+'/#spaces/'+r.s.space_id+'/sources')
        page.fill('#username','admin');page.fill('#password',r.s.password);page.click('#login-form button')
        page.locator('[data-ws-action="deactivate-source"][data-id="'+r.folder['id']+'"]').click()
        expect(page.locator('#workspace-form')).to_contain_text('停用不会确认远端进程已经停止')
        saved=save(page,r,r.space_path+'/sources/'+r.folder['id'],'PUT')
        assert not saved['enabled'] and saved['version']==r.folder['version']+1
        assert saved['mapping_ids']==r.folder['mapping_ids']
        expect(page.locator('[data-ws-action="deactivate-source"][data-id="'+r.folder['id']+'"]').first).to_have_count(0)
        denied=r.s.client.get(r.space_path+'/sources/'+r.folder['id']+'/browse')
        assert denied.status_code==403,denied.text
        assert (r.s.lumen/'README.md').is_file()
    finally:
        page.context.close()
