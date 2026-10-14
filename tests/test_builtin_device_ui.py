"""Built-in lifecycle and roots editor against browser HTTP fixtures (no deployment)."""
import copy
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture
def roots_api():
    return {
        'settings': {'roots': [], 'revision': 0, 'applied_revision': 0, 'status': 'applied'},
        'requests': [],
        'get_error': None,
        'put_error': None,
    }


@pytest.fixture(params=['chromium', 'webkit'])
def builtin_page(request, roots_api):
    # Load authored scripts directly; the coordinator regenerates assets separately.
    with sync_playwright() as playwright:
        browser = getattr(playwright, request.param).launch()
        page = browser.new_page()

        def serve(route):
            req = route.request
            path = req.url.split('codepier.test', 1)[-1]
            if not path.startswith('/api/'):
                route.fulfill(content_type='text/html', body='<div id="page"></div><div id="dialogs"></div>')
                return
            data = req.post_data_json if req.post_data else None
            roots_api['requests'].append((req.method, path, data, req.headers))
            status, payload = 200, {'ok': True}
            if path.endswith('/roots'):
                error = roots_api['get_error' if req.method == 'GET' else 'put_error']
                if error:
                    status, payload = error
                elif req.method == 'PUT':
                    if data['revision'] != roots_api['settings']['revision']:
                        status, payload = 409, {'error': {'message': '目录授权版本已变化'}}
                    else:
                        roots_api['settings'] = {
                            'roots': [dict(root, allow_tasks=False) for root in data['roots']],
                            'revision': data['revision'] + 1,
                            'applied_revision': roots_api['settings']['applied_revision'],
                            'status': 'applying',
                        }
                        payload = copy.deepcopy(roots_api['settings'])
                else:
                    payload = copy.deepcopy(roots_api['settings'])
            elif path.endswith('/rotate'):
                payload = {'ok': True, 'builtin': True}
            route.fulfill(status=status, content_type='application/json', body=json.dumps(payload))

        page.route('https://codepier.test/**', serve)
        page.goto('https://codepier.test/')
        page.add_script_tag(content="""
          const S={devices:[],projects:[],session:{csrf:'csrf-fixture'},space_id:'personal',page:'devices'};
          const $=(q,root=document)=>root.querySelector(q);
          const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          const icon=()=>'',onlineBadge=v=>v?'在线':'离线',shortTime=()=>'',timeText=()=>'',json=JSON.stringify;
          const heading=()=>'',empty=()=>'',uiHelp=()=>'',buttons=(id,label)=>`<button id="${id}">${label}</button>`;
          const panelActions={register(){}},badge=v=>esc(v),notice=v=>v;
          let confirmations=[],toasts=[],pairings=0;
          const toast=v=>toasts.push(v),confirm=v=>(confirmations.push(v),true);
          const busy=async(button,fn)=>fn(),renderPage=async()=>{},loadBasics=async()=>{};
          const invalidateBasics=()=>{},networkState=()=>{},pause=ms=>new Promise(r=>setTimeout(r,ms));
          const sessionChanged=()=>new Error('Session changed'),endSession=()=>{S.session=null;};
          const uid=()=>String(Math.random());
          const closeModal=dialog=>{S.modalCleanup?.();S.modalCleanup=null;(dialog||$('.modal'))?.remove();};
          const modal=(title,body,footer)=>{
            closeModal();
            $('#dialogs').innerHTML=`<div class="modal"><h2>${title}</h2>${body}${footer}</div>`;
            return $('.modal');
          };
          document.addEventListener('click',event=>{
            const button=event.target.closest('[data-action="add-project"]');
            if(button&&!button.disabled)projectModal(null,button.dataset.device);
          });
        """)
        root = Path(__file__).resolve().parents[1]
        page.add_script_tag(path=str(root / 'web' / 'agent-install.js'))
        source = (root / 'web' / 'app.js').read_text()
        # Include the real API helper, renderers, roots editor and project dialog.
        page.add_script_tag(content=source[source.index('async function api('):source.index('const tool =')])
        start = source.index('function devicesHTML()')
        end = source.index('function resetWork(', start)
        page.add_script_tag(content=source[start:end])
        page.evaluate("""() => {
          pairingModal=()=>pairings++;
          S.devices=[{id:'builtin',name:'CodePier Hub',builtin:true,can_configure_roots:true,
            enabled:true,online:true,project_count:0,
            info:{roots:[],version:'old',platform:'Linux'},agent:{version:'old',target_version:'new',
            actions:['agent_update','agent_restart','agent_uninstall'],can_update:true,can_restart:true,
            can_uninstall:true,update_available:true}}];
          $('#page').innerHTML=devicesHTML();
        }""")
        yield page
        browser.close()


def assert_no_standalone_controls(page, selector):
    for action in ('agent-update', 'agent-restart', 'agent-uninstall', 'agent-repair', 'agent-commands'):
        expect(page.locator(f'{selector} [data-action="{action}"]')).to_have_count(0)


def writes(roots_api):
    return [req for req in roots_api['requests'] if req[0] != 'GET']


def add_root(page, path='/workspace/demo', mode='read'):
    page.click('#add-builtin-root')
    row = page.locator('.builtin-root-editor').last
    row.locator('input').fill(path)
    row.locator('select').select_option(mode)
    return row


def test_builtin_badge_managed_note_and_empty_roots(builtin_page):
    page = builtin_page
    expect(page.locator('#page')).to_contain_text('Built-in')
    expect(page.locator('#page')).to_contain_text('Hub 部署管理')
    expect(page.locator('#page')).to_contain_text('而非宿主机')
    assert_no_standalone_controls(page, '#page')
    expect(page.locator('#page')).not_to_contain_text('未纳入一键管理')
    page.locator('.device-roots summary').click()
    expect(page.locator('.device-root-list')).to_contain_text('先将目录挂载到 Hub')
    page.evaluate("deviceDetail('builtin')")
    assert_no_standalone_controls(page, '.modal')
    expect(page.locator('.modal')).to_contain_text('不会自动创建项目映射')
    expect(page.locator('.modal')).to_contain_text('私有本地进程管道')
    expect(page.locator('.modal')).not_to_contain_text('python -m agent')
    expect(page.locator('#toggle-device')).to_be_visible()
    expect(page.locator('#rename-device')).to_be_visible()
    expect(page.locator('#builtin-roots')).to_contain_text('不开启 Shell 或任务')
    expect(page.locator('#builtin-roots')).to_contain_text('不会创建目录或挂载')
    expect(page.locator('#map-builtin-project')).to_be_disabled()


def test_builtin_rotation_ack_and_persistent_delete_warning(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    page.click('#rotate-device')
    expect(page.locator('.modal')).to_have_count(0)
    assert '由 Hub 自动更新凭据并重新连接' in page.evaluate('confirmations[0]')
    assert page.evaluate('pairings') == 0
    assert writes(roots_api)[0][:2] == ('POST', '/api/devices/builtin/rotate')
    assert '自动重新连接' in page.evaluate('toasts[0]')
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('.danger-zone')).to_contain_text('Hub 重启后不会自动重建')
    page.click('#delete-device')
    expect(page.locator('.modal')).to_have_count(0)
    assert '持久保留删除决定' in page.evaluate('confirmations[1]')
    assert 'Hub 重启后不会自动重建' in page.evaluate('confirmations[1]')
    assert writes(roots_api)[1][:2] == ('DELETE', '/api/devices/builtin')


def test_builtin_direct_ui_entrypoints_block_and_remote_controls_remain(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("""() => {
      const d=S.devices[0];
      agentInstallSetup(d);showAgentInstallCommand(d,{});showAgentMaintenanceCommands(d);
      for(const action of ['agent_update','agent_restart','agent_uninstall'])agentLifecycleModal(d,action);
    }""")
    expect(page.locator('.modal')).to_have_count(0)
    assert not roots_api['requests']
    page.evaluate("""() => {
      S.devices[0].builtin=false;
      $('#page').innerHTML=devicesHTML();
    }""")
    expect(page.locator('#page [data-action="agent-update"]')).to_have_count(1)
    expect(page.locator('#page [data-action="agent-restart"]')).to_have_count(1)
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('.modal [data-action="agent-uninstall"]')).to_have_count(1)
    expect(page.locator('.modal [data-action="agent-commands"]')).to_have_count(1)
    expect(page.locator('#builtin-roots')).to_have_count(0)
    assert not roots_api['requests']
    page.evaluate("""() => {
      closeModal($('.modal'));
      S.devices[0].online=false;S.devices[0].agent.actions=[];
      $('#page').innerHTML=devicesHTML();
    }""")
    expect(page.locator('#page [data-action="agent-repair"]')).to_have_count(1)


@pytest.mark.parametrize('permission', [False, None])
def test_roots_editor_requires_explicit_permission(builtin_page, roots_api, permission):
    page = builtin_page
    page.evaluate('(permission)=>{S.devices[0].can_configure_roots=permission;}', permission)
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('#builtin-roots')).to_have_count(0)
    assert not roots_api['requests']


def test_roots_save_explicit_modes_apply_and_map(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    page.click('#add-builtin-root')
    row = page.locator('.builtin-root-editor')
    expect(row.locator('select')).to_have_value('read')
    row.locator('input').fill('/workspace/readonly')
    add_root(page, '/workspace/editable', 'write')
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-status')).to_contain_text('正在应用')
    req = writes(roots_api)[0]
    assert req[:3] == ('PUT', '/api/devices/builtin/roots', {
        'roots': [{'path': '/workspace/readonly', 'writable': False},
                  {'path': '/workspace/editable', 'writable': True}],
        'revision': 0,
    })
    assert req[3]['x-rd-csrf'] == 'csrf-fixture'
    assert req[3]['x-codepier-space'] == 'personal'
    expect(page.locator('#builtin-roots-status')).to_contain_text('已保存版本 1 · 已应用版本 0')
    expect(page.locator('#map-builtin-project')).to_be_disabled()
    roots_api['settings'].update(applied_revision=1, status='applied')
    expect(page.locator('#builtin-roots-status')).to_contain_text('已生效', timeout=6000)
    page.click('#map-builtin-project')
    expect(page.locator('#project-form')).to_be_visible()
    expect(page.locator('#project-form [name="device_id"]')).to_have_value('builtin')
    expect(page.locator('#project-form [name="root"]')).to_have_value('')
    expect(page.locator('#project-form [name="allow_tasks"]')).not_to_be_checked()
    expect(page.locator('#project-form [name="allow_tasks"]')).to_be_disabled()
    expect(page.locator('#project-form [name="mode"]')).to_have_value('read')
    assert len(writes(roots_api)) == 1  # Mapping is explicit, not created by saving roots.
    page.locator('#project-form [name="alias"]').fill('demo')
    page.locator('#project-form [name="root"]').fill('/workspace/readonly')
    page.click('#save-project')
    expect(page.locator('.modal')).to_have_count(0)
    assert writes(roots_api)[1][:2] == ('POST', '/api/projects')
    assert writes(roots_api)[1][2]['allow_tasks'] is False
    assert writes(roots_api)[1][2]['mode'] == 'read'


def test_roots_remove_save_empty_and_increment_revision(builtin_page, roots_api):
    roots_api['settings'].update(roots=[{'path': '/workspace', 'writable': True, 'allow_tasks': False}],
                                 revision=8, applied_revision=8)
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('.builtin-root-editor select')).to_have_value('write')
    page.locator('.builtin-root-editor button').click()
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-status')).to_contain_text('已保存版本 9')
    assert writes(roots_api)[0][2] == {'roots': [], 'revision': 8}
    expect(page.locator('#map-builtin-project')).to_be_disabled()


@pytest.mark.parametrize('path', ['', 'relative/project'])
def test_roots_inline_absolute_path_validation(builtin_page, roots_api, path):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    row = add_root(page, path)
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-error')).to_contain_text('绝对路径')
    expect(row.locator('input')).to_have_attribute('aria-invalid', 'true')
    assert not writes(roots_api)


def test_roots_duplicate_and_server_validation_preserve_input(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    add_root(page)
    row = add_root(page)
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-error')).to_contain_text('不能重复')
    assert not writes(roots_api)
    row.locator('input').fill('/app/data')
    roots_api['put_error'] = (403, {'error': {'code': 'PROTECTED_ROOT', 'message': 'Hub 数据库和密钥目录不可授权'}})
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-error')).to_contain_text('Hub 数据库和密钥目录不可授权')
    expect(row.locator('input')).to_have_value('/app/data')
    expect(page.locator('#save-builtin-roots')).to_be_enabled()
    row.locator('input').fill('/workspace/allowed')
    roots_api['put_error'] = None
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-status')).to_contain_text('已保存版本 1')


@pytest.mark.parametrize('method', ['GET', 'PUT'])
def test_roots_authorization_errors_inline_and_disable_edits(builtin_page, roots_api, method):
    page = builtin_page
    error = (403, {'error': {'message': '需要实例管理员及节点管理权限'}})
    if method == 'GET':
        roots_api['get_error'] = error
    page.evaluate("deviceDetail('builtin')")
    if method == 'PUT':
        add_root(page)
        roots_api['put_error'] = error
        page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-error')).to_contain_text('需要实例管理员及节点管理权限')
    expect(page.locator('#add-builtin-root')).to_be_disabled()
    expect(page.locator('#save-builtin-roots')).to_be_disabled()
    expect(page.locator('#refresh-builtin-roots')).to_be_enabled()


@pytest.mark.parametrize('status', ['offline', 'disabled'])
def test_roots_saved_not_applied_for_offline_or_disabled(builtin_page, roots_api, status):
    roots_api['settings'].update(revision=3, applied_revision=None, status=status)
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('#builtin-roots-status')).to_contain_text('已保存版本 3 · 已应用版本 无')
    expect(page.locator('#builtin-roots-status')).to_contain_text('离线' if status == 'offline' else '已停用')
    expect(page.locator('#save-builtin-roots')).to_be_enabled()
    expect(page.locator('#map-builtin-project')).to_be_disabled()


def test_roots_poll_preserves_draft_and_detects_other_editor(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    row = add_root(page, '/workspace/unsaved')
    roots_api['settings'].update(revision=2, applied_revision=2,
                                 roots=[{'path': '/workspace/other', 'writable': False, 'allow_tasks': False}])
    expect(page.locator('#builtin-roots-error')).to_contain_text('其他页面更改', timeout=6000)
    expect(row.locator('input')).to_have_value('/workspace/unsaved')
    expect(page.locator('#save-builtin-roots')).to_be_disabled()
    page.click('#refresh-builtin-roots')
    expect(page.locator('.builtin-root-editor input')).to_have_value('/workspace/other')
    expect(page.locator('#save-builtin-roots')).to_be_enabled()
    assert '丢弃未保存的目录输入' in page.evaluate('confirmations[0]')


def test_roots_conflict_does_not_overwrite_newer_settings(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    add_root(page)
    roots_api['settings']['revision'] = 4
    page.click('#save-builtin-roots')
    expect(page.locator('#builtin-roots-error')).to_contain_text('重新读取最新授权')
    expect(page.locator('#save-builtin-roots')).to_be_disabled()
    expect(page.locator('.builtin-root-editor input')).to_have_value('/workspace/demo')
    assert roots_api['settings']['revision'] == 4


def test_project_device_switch_keeps_remote_execution_controls(builtin_page):
    page = builtin_page
    page.evaluate("""() => {
      S.devices.push({id:'remote',name:'Remote',builtin:false,online:true});
      return projectModal(null,'remote');
    }""")
    tasks = page.locator('#project-form [name="allow_tasks"]')
    mode = page.locator('#project-form [name="mode"]')
    expect(tasks).to_be_enabled()
    expect(mode).to_have_value('write')
    tasks.check()
    mode.select_option('read')
    page.locator('#project-form [name="device_id"]').select_option('builtin')
    expect(tasks).not_to_be_checked()
    expect(tasks).to_be_disabled()
    expect(mode).to_have_value('read')
    page.locator('#project-form [name="device_id"]').select_option('remote')
    expect(tasks).to_be_enabled()
    expect(mode).to_have_value('read')  # Keep the user's remote mode, never widen it.


def test_roots_poll_stops_when_dialog_closes(builtin_page, roots_api):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    page.evaluate("closeModal($('.modal'))")
    count = len(roots_api['requests'])
    page.wait_for_timeout(3300)
    assert len(roots_api['requests']) == count
