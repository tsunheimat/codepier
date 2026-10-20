"""Actual browser pages using the real workspace HTTP router and mock upstream.

The ASGI adapter only replaces the loopback HTTP transport, never authorization,
URLs, payloads or database policy. All accounts, cookies and secrets are synthetic.
"""
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.test_mcp_gateway import gw, team, configured, call, result  # noqa: F401
from tests.identity_fixtures import must, make_profile, make_connection, set_member

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.fixture(params=['chromium','webkit'])
def gateway_browser(request, chat_browser_pool):
    return chat_browser_pool(request.param)


@contextmanager
def workspace_page(gw, browser, *, width=1280, workspace='team'):
    app,b,_=gw
    context=browser.new_context(viewport={'width':width,'height':900})
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.add_init_script('''
      window.fixtureEventStreams=[];
      const originalFetch=window.fetch.bind(window);
      window.fixtureHoldInventory=false;
      window.fixtureHoldProfiles=false;
      window.fetch=async(...args)=>{
        const response=await originalFetch(...args);
        const path=new URL(response.url).pathname;
        if(window.fixtureHoldInventory && response.ok && path==='/api/spaces/team/mcp-gateway'){
          window.fixtureHoldInventory=false;
          await new Promise(resolve=>window.fixtureReleaseInventory=resolve);
        }
        if(window.fixtureHoldProfiles && response.ok && path==='/api/spaces/team/profiles'){
          window.fixtureHoldProfiles=false;
          await new Promise(resolve=>window.fixtureReleaseProfiles=resolve);
          window.fixtureProfilesDelivered=true;
        }
        return response;
      };
      // SSE lifecycle itself has separate integration coverage. Avoid a fake
      // zero-length SSE stream racing page teardown; retain its intended URL.
      window.EventSource=class extends EventTarget {
        static CONNECTING=0; static OPEN=1; static CLOSED=2;
        constructor(url){
          super();const parsed=new URL(url,location.href);
          if(parsed.origin!==location.origin || parsed.pathname!=='/api/events')
            throw new Error('Unexpected EventSource destination');
          this.url=parsed.href;this.readyState=1;fixtureEventStreams.push(this);
        }
        close(){this.readyState=2;}
      };
    ''')
    def route(item):
        request=item.request
        parts=urlsplit(request.url)
        if parts.scheme!='http' or parts.hostname!='testserver':
            item.abort();return
        response=b['owner'].client.request(request.method,request.url,
            headers=request.all_headers(),content=request.post_data_buffer,follow_redirects=False)
        headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')}
        item.fulfill(status=response.status_code,headers=headers,body=response.content)
    page.route('**/*',route)
    try:
        page.goto('http://testserver/#spaces/'+workspace+'/mcp')
        expect(page.locator('#login-form')).to_be_visible()
        page.fill('#username','owner');page.fill('#password','fixture-password-only')
        page.click('#login-form button')
        expect(page.locator('#gateway-page')).to_be_visible(timeout=15000)
        yield page,errors
    finally:
        context.close()


@pytest.mark.parametrize('width',[1280,390])
def test_gateway_panel_end_to_end(gw,gateway_browser,width,tmp_path):
    app,b,backend=gw
    profile=make_profile(b['owner'],{},label='UI workspace profile')
    with workspace_page(gw,gateway_browser,width=width) as (page,errors):
        page.click('[data-gw="connector"]')
        page.fill('#gw-form [name="label"]','UI MCP <not markup>')
        page.fill('#gw-form [name="endpoint"]','https://mcp.example/mcp')
        page.evaluate('fixtureHoldInventory=true')
        page.click('button[form="gw-form"]')
        page.wait_for_function('() => typeof fixtureReleaseInventory === "function"')
        expect(page.locator('#gw-form')).to_be_visible()
        expect(page.locator('button[form="gw-form"]')).to_be_disabled()
        assert len(app.state.store.all('SELECT id FROM gateway_connectors'))==1
        page.evaluate('fixtureReleaseInventory()')
        expect(page.locator('#gw-form')).to_have_count(0)
        expect(page.locator('#gateway-page')).to_contain_text('UI MCP <not markup>')
        page.click('[data-gw="account"]')
        page.fill('#gw-form [name="label"]','UI account')
        page.fill('#gw-form [name="token"]','PRIVATE_UI_TOKEN')
        page.check('#gw-form [name="shared"]')
        page.evaluate('fixtureHoldInventory=true;fixtureReleaseInventory=null')
        page.click('button[form="gw-form"]')
        page.wait_for_function('() => typeof fixtureReleaseInventory === "function"')
        expect(page.locator('#gw-form [name="token"]')).to_have_value('')
        expect(page.locator('button[form="gw-form"]')).to_be_disabled()
        page.evaluate("document.querySelector('#gw-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
        page.evaluate('fixtureReleaseInventory()')
        expect(page.locator('#gw-form')).to_have_count(0)
        assert len(app.state.store.all('SELECT id FROM gateway_accounts'))==1
        page.click('[data-gw-discover]')
        page.fill('#gw-form [name="alias"]','ui_mcp')
        page.check('#gw-form [name="tool"][value="echo"]')
        page.check('#gw-form [name="confirmed"]')
        page.click('button[form="gw-form"]');expect(page.locator('#gw-form')).to_have_count(0)
        page.click('#section-tab-tools')
        page.click('[data-gw-profile]')
        page.select_option('#gw-form [name="profile_id"]',profile['id'])
        page.check('#gw-form [name="tool"][value="echo"]')
        page.click('button[form="gw-form"]');expect(page.locator('#gw-form')).to_have_count(0)
        page.click('#section-tab-tools')
        page.click('[data-gw-profile]')
        expect(page.locator('#gw-form [name="tool"][value="echo"]')).to_be_checked()
        page.get_by_role('button',name='取消',exact=True).click()
        profile=must(b['owner'].get('/api/spaces/team/profiles/'+profile['id']))
        assert profile['policy']['connector_rules'][0]['tools']==['echo']
        member=next(m for m in must(b['owner'].get('/api/spaces/team/members'))['members']
                    if m['user_id']=='owner' and m['source']=='manual')
        set_member(b['owner'],'owner',{**member['policy'],'connector_rules':profile['policy']['connector_rules']})
        connection=make_connection(b['owner'],profile)
        page.evaluate('renderPage(false)')
        selector='[data-gw-consent="'+connection['connection_id']+'"]'
        page.click('#section-tab-delegation')
        expect(page.locator(selector)).to_be_visible()
        page.click(selector)
        expect(page.locator('#gw-form')).to_contain_text(profile['label'])
        page.check('#gw-form [name="confirmed"]')
        page.click('button[form="gw-form"]');expect(page.locator('#gw-form')).to_have_count(0)
        assert app.state.store.one('SELECT consent_version FROM gateway_consents WHERE grant_id=?',
                                  (connection['connection_id'],))['consent_version']==1
        out=result(call(b['owner'],connection['token'],'ui_mcp__echo',{'value':'UI configured'}))
        assert out['structuredContent']['value']=='UI configured'
        assert backend.effects==1
        assert 'PRIVATE_UI_TOKEN' not in page.locator('body').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.evaluate("!Object.keys(sessionStorage).some(k=>k.startsWith('codepier-space:'))")
        assert page.evaluate("fixtureEventStreams.every(s=>!new URL(s.url).searchParams.has('space_id'))")
        page.screenshot(path=str(tmp_path/'gateway-workspace.png'),full_page=True)
        assert errors==[]


def test_delayed_profile_lookup_does_not_open_editor_in_another_workspace(gw,gateway_browser):
    configured(gw)
    with workspace_page(gw,gateway_browser) as (page,errors):
        page.evaluate('fixtureHoldProfiles=true')
        page.click('#section-tab-tools')
        page.click('[data-gw-profile]')
        page.wait_for_function('() => typeof fixtureReleaseProfiles === "function"')
        page.evaluate('navigate("spaces/other-team/mcp")')
        expect(page).to_have_url('http://testserver/#spaces/other-team/mcp')
        expect(page.locator('#workspace-page')).to_contain_text('Development other-team')
        page.evaluate('fixtureReleaseProfiles()')
        page.wait_for_function('() => fixtureProfilesDelivered === true')
        page.evaluate('async()=>{await Promise.resolve();await new Promise(requestAnimationFrame);}')
        expect(page.locator('#gw-form')).to_have_count(0)
        assert errors==[]


def test_delayed_profile_lookup_does_not_replace_a_new_account_draft(gw,gateway_browser):
    configured(gw)
    with workspace_page(gw,gateway_browser) as (page,errors):
        page.click('#section-tab-tools')
        page.evaluate('''() => {
          fixtureHoldProfiles=true;
          const button=document.querySelector('[data-gw-profile]');
          const start=button.onclick;
          window.fixtureProfileActionFinished=false;
          button.onclick=function(...args){
            const result=start.apply(this,args);
            Promise.resolve(result).finally(()=>window.fixtureProfileActionFinished=true);
            return result;
          };
        }''')
        page.click('[data-gw-profile]')
        page.wait_for_function('() => typeof fixtureReleaseProfiles === "function"')
        page.click('[data-gw="account"]')
        page.fill('#gw-form [name="label"]','Keep this unsaved account draft')
        page.evaluate('fixtureReleaseProfiles()')
        page.wait_for_function('() => fixtureProfileActionFinished === true')
        expect(page.locator('#modal-title')).to_have_text('连接后端帐户')
        expect(page.locator('#gw-form [name="label"]')).to_have_value('Keep this unsaved account draft')
        assert errors==[]
