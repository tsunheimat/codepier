"""Built-in device lifecycle UI in Chromium and WebKit (no installer execution)."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(params=['chromium', 'webkit'])
def builtin_page(request):
    # Load the authored scripts directly: no generated asset writes are needed.
    with sync_playwright() as playwright:
        browser = getattr(playwright, request.param).launch()
        page = browser.new_page()
        page.goto('about:blank')
        page.set_content('<div id="page"></div><div id="dialogs"></div>')
        page.add_script_tag(content="""
          const S={devices:[],projects:[],session:{},page:'devices'};
          const $=(q,root=document)=>root.querySelector(q);
          const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          const icon=()=>'',onlineBadge=v=>v?'在线':'离线',shortTime=()=>'',timeText=()=>'',json=JSON.stringify;
          const heading=()=>'',empty=()=>'',uiHelp=()=>'',buttons=(id,label)=>`<button id="${id}">${label}</button>`;
          const panelActions={register(){}};
          const badge=v=>esc(v),notice=v=>v;
          let confirmations=[],requests=[],toasts=[],pairings=0;
          const toast=v=>toasts.push(v),confirm=v=>(confirmations.push(v),true);
          const busy=async(button,fn)=>fn(),renderPage=async()=>{};
          const post=async(url)=>{requests.push(url);return {ok:true,builtin:true};};
          const api=async(url,options)=>{requests.push([url,options.method]);return {ok:true};};
          const modal=(title,body,footer)=>{document.querySelector('#dialogs').innerHTML=`<div class="modal"><h2>${title}</h2>${body}${footer}</div>`;return $('.modal');};
          const closeModal=dialog=>dialog.remove();
        """)
        root = Path(__file__).resolve().parents[1]
        page.add_script_tag(path=str(root / 'web' / 'agent-install.js'))
        # app.js has startup side effects; compile only its device renderers and
        # dialog from source, rather than copying their implementation into tests.
        source = (root / 'web' / 'app.js').read_text()
        start = source.index('function devicesHTML()')
        end = source.index('async function projectModal(', start)
        page.add_script_tag(content=source[start:end])
        page.evaluate("""() => {
          pairingModal=()=>pairings++;
          S.devices=[{id:'builtin',name:'CodePier Hub',builtin:true,enabled:true,online:true,project_count:0,
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


def test_builtin_badge_managed_note_and_empty_roots(builtin_page):
    page = builtin_page
    expect(page.locator('#page')).to_contain_text('Built-in')
    expect(page.locator('#page')).to_contain_text('Hub 部署管理')
    expect(page.locator('#page')).to_contain_text('而非宿主机')
    assert_no_standalone_controls(page, '#page')
    expect(page.locator('#page')).not_to_contain_text('未纳入一键管理')
    page.locator('.device-roots summary').click()
    expect(page.locator('.device-root-list')).to_contain_text('默认不开放项目目录')
    page.evaluate("deviceDetail('builtin')")
    assert_no_standalone_controls(page, '.modal')
    expect(page.locator('.modal')).to_contain_text('不会自动创建项目映射')
    expect(page.locator('.modal')).not_to_contain_text('python -m agent')
    expect(page.locator('#toggle-device')).to_be_visible()
    expect(page.locator('#rename-device')).to_be_visible()


def test_builtin_rotation_ack_and_persistent_delete_warning(builtin_page):
    page = builtin_page
    page.evaluate("deviceDetail('builtin')")
    page.click('#rotate-device')
    assert '由 Hub 自动更新凭据并重新连接' in page.evaluate('confirmations[0]')
    assert page.evaluate('pairings') == 0
    assert page.evaluate('requests') == ['/api/devices/builtin/rotate']
    assert '自动重新连接' in page.evaluate('toasts[0]')
    expect(page.locator('.modal')).to_have_count(0)
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('.danger-zone')).to_contain_text('Hub 重启后不会自动重建')
    page.click('#delete-device')
    assert '持久保留删除决定' in page.evaluate('confirmations[1]')
    assert 'Hub 重启后不会自动重建' in page.evaluate('confirmations[1]')
    assert page.evaluate('requests[1]') == ['/api/devices/builtin', 'DELETE']


def test_builtin_direct_ui_entrypoints_block_and_remote_controls_remain(builtin_page):
    page = builtin_page
    page.evaluate("""() => {
      const d=S.devices[0];
      agentInstallSetup(d);showAgentInstallCommand(d,{});showAgentMaintenanceCommands(d);
      for(const action of ['agent_update','agent_restart','agent_uninstall'])agentLifecycleModal(d,action);
    }""")
    expect(page.locator('.modal')).to_have_count(0)
    assert page.evaluate('requests.length') == 0
    page.evaluate("""() => {
      S.devices[0].builtin=false;
      $('#page').innerHTML=devicesHTML();
    }""")
    expect(page.locator('#page [data-action="agent-update"]')).to_have_count(1)
    expect(page.locator('#page [data-action="agent-restart"]')).to_have_count(1)
    page.evaluate("deviceDetail('builtin')")
    expect(page.locator('.modal [data-action="agent-uninstall"]')).to_have_count(1)
    expect(page.locator('.modal [data-action="agent-commands"]')).to_have_count(1)
    page.evaluate("""() => {
      closeModal($('.modal'));
      S.devices[0].online=false;S.devices[0].agent.actions=[];
      $('#page').innerHTML=devicesHTML();
    }""")
    expect(page.locator('#page [data-action="agent-repair"]')).to_have_count(1)
