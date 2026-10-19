"""Synthetic-browser checks for stale workspace content and scoped exports."""
from pathlib import Path

import pytest
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["chromium", "webkit"])
def privacy_page(request, chat_browser_pool):
    page = chat_browser_pool(request.param).new_page()
    page.route("https://privacy.fixture/**", lambda route: route.fulfill(
        status=200, content_type="text/html",
        body='<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>'))
    page.goto("https://privacy.fixture/")
    for name in ("core/bundle.js", "ui.js"):
        page.add_script_tag(path=str(ROOT / "web" / name))
    page.add_script_tag(content=panel_without_boot())
    page.add_script_tag(path=str(ROOT / "web/identity.js"))
    page.evaluate("""() => {
      S.session={user_id:'fixture-owner',username:'Fixture'};
      S.page='workbench';location.hash='#spaces/team/projects';
      window.fetch=async()=>new Response(JSON.stringify({providers:[]}),
        {status:200,headers:{'Content-Type':'application/json'}});
      window.EventSource=class {
        constructor(){this.listeners={};window.fixtureEvents=this;}
        addEventListener(name,handler){this.listeners[name]=handler;}
        close(){this.closed=true;}
      };
    }""")
    try:
        yield page
    finally:
        page.context.close()


def test_logout_clears_all_space_snapshots(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      for(const key of ['overview','projects','devices','settings','grants'])
        S[key]=[{private:'PRIVATE_SENTINEL'}];
      endSession(true);
    }""")
    assert "PRIVATE_SENTINEL" not in page.evaluate("JSON.stringify(S)")
    assert page.locator("#login-form").is_visible()


def test_sse_revocation_clears_loaded_private_content(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      S.work.content='PRIVATE_SENTINEL';S.work.original='PRIVATE_SENTINEL';
      document.querySelector('#app').textContent='PRIVATE_SENTINEL';
      connectEvents();
      fixtureEvents.listeners.session_revoked?.({data:'{}'});
    }""")
    assert "PRIVATE_SENTINEL" not in page.locator("body").inner_text()
    assert page.evaluate("S.work.content") == ""
    assert page.locator("#login-form").is_visible()


def test_audit_download_is_account_scoped_without_an_ambient_space(privacy_page):
    page=privacy_page
    page.evaluate("""async () => {
      window.downloads=[];
      HTMLAnchorElement.prototype.click=function(){downloads.push(this.href);};
      await panelActions.dispatch('export-audit',document.createElement('button'),{});
    }""")
    from urllib.parse import parse_qs, urlsplit
    query=parse_qs(urlsplit(page.evaluate("downloads[0]")).query)
    assert "space_id" not in query
    assert page.evaluate("Object.hasOwn(S,'space_id')") is False


def test_stale_stream_cannot_clear_new_space(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      connectEvents();window.oldEvents=fixtureEvents;
      S.session={user_id:'fixture-owner'};location.hash='#spaces/new-team/projects';
      S.work.content='NEW_SPACE_DRAFT';
      oldEvents.listeners.session_revoked?.({data:'{}'});oldEvents.listeners.access_changed?.({data:'{}'});
    }""")
    assert page.evaluate("S.work.content") == "NEW_SPACE_DRAFT"
    assert page.evaluate("location.hash") == "#spaces/new-team/projects"


@pytest.mark.parametrize("status,code,revoked", [
    (403, "SPACE_FORBIDDEN", True), (404, "SPACE_NOT_FOUND", True),
    (401, "LOGIN_REQUIRED", True), (503, "TEMPORARILY_UNAVAILABLE", False),
])
def test_rejected_reconnect_rechecks_authority_without_erasing_on_network_failure(privacy_page, status, code, revoked):
    page=privacy_page
    page.evaluate("""({status,code}) => {
      S.work.content='PRIVATE_SENTINEL';
      window.fetch=async(path)=>path==='/api/auth/providers'
        ? new Response(JSON.stringify({providers:[]}),{status:200})
        : path==='/api/session'
          ? new Response(JSON.stringify({authenticated:status!==401}),{status:200})
          : new Response(JSON.stringify({error:{code,message:'fixture'}}),
            {status,headers:{'Content-Type':'application/json'}});
      connectEvents();fixtureEvents.onerror();
    }""", {"status": status, "code": code})
    if revoked and status == 401:
        page.wait_for_function("S.session===null")
        assert page.evaluate("S.work.content") == ""
    elif revoked:
        page.wait_for_function("document.querySelector('[data-action=retry-identity]')!==null")
        assert page.evaluate("S.session!==null")
        assert "PRIVATE_SENTINEL" not in page.locator("body").inner_text()
        assert not page.locator("#login-form").count()
    else:
        page.wait_for_function("document.querySelector('#toasts')!==null")
        assert page.evaluate("S.work.content") == "PRIVATE_SENTINEL"
        assert page.evaluate("S.session!==null")


def test_audit_download_needs_no_workspace_selector(privacy_page):
    page=privacy_page
    result=page.evaluate("""async () => {
      window.downloads=[];
      HTMLAnchorElement.prototype.click=function(){downloads.push(this.href);};
      try { await panelActions.dispatch('export-audit',document.createElement('button'),{}); }
      catch(error) { return {error:error.message,downloads}; }
      return {downloads};
    }""")
    assert len(result["downloads"])==1
    assert "/api/audit-export" in result["downloads"][0]
    assert "space_id" not in result["downloads"][0]
    assert "error" not in result


@pytest.mark.parametrize("event", ["access_changed"])
@pytest.mark.parametrize("project", ["kept", "removed"])
def test_policy_change_resynchronizes_without_login_or_mutation_replay(privacy_page, event, project):
    page = privacy_page
    page.evaluate("""({event,project}) => {
      const mapping=id=>({id,alias:id,root:'/'+id,device_id:'device',mode:'write'});
      S.projects=[mapping('kept'),mapping('removed')];
      Object.assign(S.work,{project,content:'DRAFT_'+project,original:'BASE',dirty:true});
      document.querySelector('#app').textContent='PRIVATE_OLD_VIEW';
      window.requests=[];
      window.fetch=async(path,options)=>{
        requests.push({path,method:options?.method||'GET'});
        const value=path==='/api/iam/me'
          ? {id:'fixture-owner',instance_admin:false,spaces:[{id:'team',kind:'workspace',management:[]}]}
          : path==='/api/projects'?{projects:[mapping('kept')]}
          : path==='/api/devices'?{devices:[]}
          : path==='/api/session'?{authenticated:true,user_id:'fixture-owner'}
          : {providers:[]};
        return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
      };
      // Presentation adapter only: run the real invalidation, identity,
      // request and suspended-work revalidation pipeline.
      renderShell=()=>{document.querySelector('#app').innerHTML='<div id="page"></div>';};
      renderPage=async()=>{document.querySelector('#page').textContent=S.work.content;window.recovered=true;};
      connectEvents();window.oldEvents=fixtureEvents;
      oldEvents.listeners[event]({data:'{}'});
      window.hiddenImmediately=!document.body.textContent.includes('PRIVATE_OLD_VIEW');
    }""", {"event": event, "project": project})
    assert page.evaluate("hiddenImmediately")
    page.wait_for_function("window.recovered===true")
    assert page.evaluate("S.session.user_id") == 'fixture-owner'
    assert page.evaluate("Object.hasOwn(S,'space_id')") is False
    assert page.evaluate("S.work.content") == ('DRAFT_kept' if project == 'kept' else '')
    assert not page.locator('#login-form').count()
    assert all(r['method'] == 'GET' and r['path'] not in {'/api/login', '/api/logout'} for r in page.evaluate('requests'))
    assert page.evaluate('oldEvents.closed')


def test_late_reply_cannot_restore_revoked_content(privacy_page):
    page = privacy_page
    page.evaluate("""() => {
      window.fetch=()=>new Promise(resolve=>{window.oldReply=resolve;});
      window.oldResult=api('/api/projects').then(()=> 'APPLIED',e=>e.code);
      S.work.content='PRIVATE_OLD_VIEW';document.querySelector('#app').textContent=S.work.content;
      connectEvents();
      // Hold recovery offline; old private content must stay hidden.
      window.fetch=async()=>{throw Error('offline');};
      fixtureEvents.listeners.access_changed({data:'{}'});
      oldReply(new Response(JSON.stringify({projects:[{id:'revoked'}]}),{status:200}));
    }""")
    assert page.evaluate('oldResult') == 'SESSION_CHANGED'
    assert 'PRIVATE_OLD_VIEW' not in page.locator('body').inner_text()
    assert page.evaluate('S.session!==null')


def test_global_session_revocation_is_not_ignored_after_workspace_navigation(privacy_page):
    page = privacy_page
    page.evaluate("""() => {
      connectEvents();const previous=fixtureEvents;
      location.hash='#spaces/other/projects';S.work.content='CURRENT_DRAFT';
      previous.listeners.session_revoked({data:'{}'});
      previous.listeners.access_changed({data:'{}'});
    }""")
    assert page.evaluate('S.work.content')==''
    assert page.evaluate('S.session') is None
    assert page.locator('#login-form').is_visible()



def test_repeated_suspension_preserves_original_project_mapping(privacy_page):
    page = privacy_page
    page.evaluate("""async () => {
      S.projects=[{id:'p',root:'/original',device_id:'d'}];
      Object.assign(S.work,{project:'p',content:'OLD_MAPPING_DRAFT',dirty:true});
      suspendPanelContext(false,true);
      suspendPanelContext(false,true);
      window.quarantinedRoot=S.suspendedMappings.get('p').root;
      window.fetch=async path=>new Response(JSON.stringify(path==='/api/projects'
        ? {projects:[{id:'p',root:'/replacement',device_id:'d'}]}:{devices:[]}),{status:200});
      await revalidateSuspendedWork();
    }""")
    assert page.evaluate('quarantinedRoot') == '/original'
    assert page.evaluate('S.work.content') == ''
    assert page.evaluate('S.session!==null')


def test_suspended_work_cannot_be_relabelled_to_another_account(privacy_page):
    page = privacy_page
    page.evaluate("""() => {
      S.work.content='OTHER_SPACE_PRIVATE';
      suspendPanelContext(false,true);
      S.session={user_id:'another-account',username:'Other'};
      suspendPanelContext(false,true);
    }""")
    assert page.evaluate('S.work.content') == ''
    assert page.evaluate('S.suspendedMappings') is None


@pytest.mark.parametrize('changed',['folder_id','subpath','space_id','missing_snapshot'])
def test_quarantined_draft_requires_the_same_folder_binding_not_just_same_path(privacy_page,changed):
    page=privacy_page
    page.evaluate("""async changed => {
      const original={id:'p',space_id:'one',folder_id:'folder-a',subpath:'app',root:'/shared/app',device_id:'d'};
      S.projects=changed==='missing_snapshot'?[]:[original];
      Object.assign(S.work,{project:'p',content:'OLD_BINDING_DRAFT',dirty:true});
      suspendPanelContext(false,true);
      const replacement={...original};
      if(changed!=='missing_snapshot') replacement[changed]='different';
      window.fetch=async path=>new Response(JSON.stringify(path==='/api/projects'
        ? {projects:[replacement]}:{devices:[]}),{status:200});
      await revalidateSuspendedWork();
    }""",changed)
    assert page.evaluate('S.work.content')==''
    assert page.evaluate('S.session!==null')


def test_retired_access_event_is_not_registered_as_a_second_protocol(privacy_page):
    names=privacy_page.evaluate("() => {connectEvents();return Object.keys(fixtureEvents.listeners);}")
    assert 'access_revoked' not in names
    assert {'access_changed','session_revoked'}<=set(names)
