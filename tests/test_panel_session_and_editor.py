"""F07/F12/F13/F16: real app functions and DOM with disposable HTTP responses.

No live account, model, project file, or browser profile is used by these tests.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_page(chat_browser_pool):
    page = chat_browser_pool('chromium').new_page(viewport={'width':1440,'height':1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('https://flow.fixture/**', lambda route: route.fulfill(
        status=200, content_type='text/html',
        body='<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>'))
    page.goto('https://flow.fixture/')
    for name in ('tokens.css','styles.css','workspace.css','chat.css'):
        page.add_style_tag(path=str(ROOT / 'web' / name))
    for name in ('core/bundle.js', 'ui.js'):
        page.add_script_tag(path=str(ROOT / 'web' / name))
    page.add_script_tag(content=panel_without_boot())
    page.add_script_tag(path=str(ROOT / 'web' / 'identity.js'))
    page.add_script_tag(path=str(ROOT / 'web' / 'product.js'))
    page.add_script_tag(path=str(ROOT / 'web' / 'spaces.js'))
    for name in ('chat-markdown.js','chat-panels.js','chat-chrome.js',
                 'chat-history.js','chat-catalog.js','chat.js'):
        page.add_script_tag(path=str(ROOT / 'web' / name))
    page.evaluate('''async () => {
      S.session={user_id:'owner-one',username:'admin',csrf:'fixture'};S.page='native';
      window.nextLogin={...S.session};window.calls=[];window.fixtureAuthenticated=true;
      window.fixtureDevices=[{id:'node-one',name:'Fixture node',online:true}];
      window.fixtureProjects=[{id:'project-one',alias:'Fixture',device_id:'node-one',
        root:'/folders/folder-one/one',mode:'write',allow_tasks:true,online:true,version:1,
        folder_id:'folder-one',subpath:'one',workspace:{id:'space-one',label:'Explicit workspace'}}];
      window.EventSource=class {addEventListener(){} close(){this.closed=true;}};
      window.fetch=async(path,options={})=>{
        const method=options.method||'GET',body=options.body?JSON.parse(options.body):{};
        calls.push({path,method,body});
        const response=(value,status=200)=>new Response(JSON.stringify(value),{status,
          headers:{'Content-Type':'application/json'}});
        if(path==='/api/fixture-expire'){
          fixtureAuthenticated=false;
          return response({error:{code:'UNAUTHORIZED',message:'Session expired'}},401);
        }
        if(path==='/api/session')return response({authenticated:fixtureAuthenticated,
          ...(fixtureAuthenticated?{user_id:S.session.user_id,csrf:S.session.csrf}:{})});
        if(path==='/api/fixture-resource-denied')return response({error:{code:'UPSTREAM_AUTH_FAILED',message:'Upstream token rejected'}},401);
        if(path==='/api/login'){fixtureAuthenticated=true;return response(nextLogin);}
        if(path==='/api/auth/providers')return response({providers:[]});
        if(path==='/api/iam/me')return response({id:S.session.user_id,username:S.session.username,
          instance_admin:true,
          spaces:[{id:'space-one',kind:'workspace',label:'Explicit workspace',active:true}],
          disabled_spaces:[],identities:[],sessions:[]});
        if(path==='/api/logout')return response({ok:true});
        if(path==='/api/devices')return response({devices:fixtureDevices});
        if((path.startsWith('/api/projects')||path==='/api/spaces/space-one/projects')&&method!=='GET'){
          if(window.saveHandler)return saveHandler({path,method,body},response);
          return response(fixtureProjects[0]);
        }
        if(path==='/api/projects')return response({projects:fixtureProjects});
        if(path==='/api/spaces')return response({spaces:[{id:'space-one',label:'Explicit workspace'}]});
        if(path==='/api/spaces/space-one')return response({id:'space-one',label:'Explicit workspace',management:['workspace']});
        if(path==='/api/spaces/space-one/projects')return response({projects:fixtureProjects});
        if(path==='/api/spaces/space-one/folders')return response({folders:[{
          id:'folder-one',name:'Reviewed source',enabled:true,mode:'write',allow_tasks:true,
          views:[{name:'Fixture node',logical_root:'/folders/folder-one'}]}]});
        if(path==='/api/projects/project-one/vps')return response({vps:[]});
        if(path.startsWith('/api/native/access?'))return window.fixtureNativeDenied
          ? response({error:{code:'CLI_FORBIDDEN',message:'Native permission revoked'}},403)
          : response({allowed:true,project_id:new URL(path,location.origin).searchParams.get('project')});
        if(path.startsWith('/api/native/sessions?'))return response({sessions:[]});
        if(path.endsWith('/chat_catalog'))return response({cli:'pi',models:[{
          id:'fixture-model',provider:'fixture',name:'Fixture Model',reasoning:true,
          input:['text','image']}],model:{provider:'fixture',id:'fixture-model'},
          thinking_levels:['off','medium'],thinkingLevel:'medium',commands:[],
          capabilities:{commands:true}});
        if(path.endsWith('/upload_list'))return response({files:[]});
        return response({});
      };
      await bootAuthenticated();
    }''')
    expect(page.locator('#chat-compose')).to_be_visible()
    try:
        yield page
        assert not errors, errors
    finally:
        page.context.close()


def expire(page):
    page.evaluate("api('/api/fixture-expire').catch(error=>error.code)")
    expect(page.locator('#login-form')).to_be_visible()


def login(page):
    page.fill('#username','admin')
    page.fill('#password','disposable-fixture-password')
    page.locator('#login-form button[type=submit]').click()
    expect(page.locator('#page')).to_be_visible()
    if page.evaluate('S.page') != 'native':
        page.evaluate("navigate('native')")
    expect(page.locator('#chat-compose')).to_be_visible()


def test_expired_session_retains_same_account_draft_files_and_unknown_receipt(app_page):
    page = app_page
    page.fill('#chat-compose','Unsent detailed implementation plan')
    page.evaluate('''() => {
      const view=chatView();window.originalView=view;
      view.files.push({file:'a'.repeat(32),name:'unsent.png',mime:'image/png',size:4,
        ready:true,preview:URL.createObjectURL(new Blob(['fixture']))});
      view.pending={id:'b'.repeat(32),receipt:'original-receipt',text:'Original request',
        attachments:[],cli:'pi',cwd:'.'};chatFiles();chatSyncChrome();
    }''')
    expire(page)
    assert page.evaluate('ChatUI.views.size') == 1
    assert page.evaluate("[...ChatUI.views.values()][0]===originalView")
    assert 'Unsent detailed' not in page.evaluate('JSON.stringify({...sessionStorage,...localStorage})')
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('Unsent detailed implementation plan')
    assert page.evaluate('chatView()===originalView')
    assert page.evaluate('chatView().files[0].name') == 'unsent.png'
    assert page.evaluate('chatView().pending.receipt') == 'original-receipt'
    assert not page.evaluate("calls.some(call=>call.path.endsWith('/start')||call.path.endsWith('/chat_prompt'))")


def test_recreated_account_with_same_username_cannot_inherit_old_draft(app_page):
    page = app_page
    page.fill('#chat-compose','private old-account draft')
    page.evaluate("S.work.content='private editor draft';S.work.dirty=true")
    expire(page)
    page.evaluate("nextLogin={user_id:'different-user',username:'admin',csrf:'new-fixture'}")
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('')
    assert not page.evaluate("[...ChatUI.views.values()].some(view=>view.draft.includes('private'))")
    assert page.evaluate('S.work.content') == ''


def test_chat_draft_warns_before_unload_and_explicit_logout_discards(app_page):
    page = app_page
    page.fill('#chat-compose','intentionally discard this draft')
    assert page.evaluate("() => {const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);return event.defaultPrevented;}")
    page.on('dialog',lambda dialog:dialog.accept())
    # The focused CLI layout hides the global sidebar; leave through navigation
    # first, retaining the draft, then use the visible account action.
    page.evaluate("navigate('resources/projects')")
    page.locator('[data-action=logout]').click()
    expect(page.locator('#login-form')).to_be_visible()
    expect(page.locator('#username')).to_have_value('')
    assert page.evaluate('ChatUI.views.size') == 0
    assert page.evaluate('S.suspendedUser') is None
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('')


def open_project(page):
    page.evaluate("async()=>{await navigate('resources/projects');await CodePierSpaces.projectEditor('project-one')}")
    expect(page.locator('#workspace-form')).to_be_visible()


def test_old_project_save_cannot_close_or_replace_new_dialog(app_page):
    page = app_page
    page.evaluate('''() => {window.saveHandler=(request,response)=>new Promise(resolve=>{
      window.finishOldSave=()=>resolve(response({id:'project-one'}));
    });}''')
    open_project(page)
    page.fill('#workspace-form [name=alias]','FirstSave')
    page.click('button[type=submit][form=workspace-form]')
    page.wait_for_function('!!window.finishOldSave')
    # Explicitly leave an in-flight editor; the late save still must not replace the next view.
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('.modal-header [data-action=close-modal]').click()
    page.evaluate("CodePierSpaces.projectEditor(null,'space-one')")
    page.fill('#workspace-form [name=alias]','SecondUnsavedMapping')
    page.fill('#workspace-form [name=subpath]','second')
    page.evaluate('finishOldSave()')
    expect(page.locator('#workspace-form [name=alias]')).to_have_value('SecondUnsavedMapping')
    expect(page.locator('#workspace-form [name=subpath]')).to_have_value('second')
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length") == 1


def test_late_project_load_cannot_open_over_a_newer_dialog(app_page):
    page = app_page
    page.evaluate('''() => {
      S.page='resources';let count=0;
      loadBasics=async()=>{if(++count===1)await new Promise(resolve=>window.finishOldLoad=resolve);};
      void projectModal('project-one');
    }''')
    page.wait_for_function('!!window.finishOldLoad')
    page.evaluate("CodePierSpaces.projectEditor(null,'space-one')")
    page.fill('#workspace-form [name=alias]','NewerDialog')
    page.evaluate('finishOldLoad()')
    expect(page.locator('#workspace-form [name=alias]')).to_have_value('NewerDialog')


def test_pending_project_validation_reuses_same_save_receipt(app_page):
    page = app_page
    page.evaluate('''() => {
      let attempts=0;
      window.saveHandler=async(request,response)=>++attempts===1
        ?response({error:{code:'VALIDATION_PENDING',message:'fixture slow validation',operation_id:'d'.repeat(32)}},409)
        :response({id:'project-one'});
    }''')
    open_project(page)
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form [role=status]')).to_contain_text('同一个保存键')
    expect(page.locator('button[type=submit][form=workspace-form]')).to_be_enabled()
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form')).to_have_count(0)
    requests = page.evaluate("calls.filter(call=>call.method==='PUT').map(call=>call.body)")
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]['idempotency_key'].startswith('workspace-')


def test_unknown_project_save_does_not_send_changed_configuration(app_page):
    page = app_page
    page.evaluate("() => {window.saveHandler=async()=>{throw new TypeError('fixture lost response')};}")
    open_project(page)
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('button[type=submit][form=workspace-form]')).to_be_disabled()
    expect(page.locator('#workspace-form [role=status]')).to_contain_text('结果不确定')
    page.fill('#workspace-form [name=alias]','DoNotReplayDifferentConfig')
    page.evaluate("document.querySelector('#workspace-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length") == 1


def test_definite_project_rejection_allows_a_fresh_explicit_validation(app_page):
    page = app_page
    page.evaluate('''() => {
      let attempts=0;
      window.saveHandler=async(request,response)=>++attempts===1
        ?response({error:{code:'LOCAL_READ_ONLY',message:'fixture permission rejected'}},403)
        :response({id:'project-one'});
    }''')
    open_project(page)
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form [role=status]')).to_contain_text('permission rejected')
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form')).to_have_count(0)
    requests = page.evaluate("calls.filter(call=>call.method==='PUT').map(call=>call.body)")
    assert len(requests) == 2
    assert requests[0]['idempotency_key'] != requests[1]['idempotency_key']
    assert {k:v for k,v in requests[0].items() if k!='idempotency_key'} == {k:v for k,v in requests[1].items() if k!='idempotency_key'}


def test_crlf_editor_changes_only_the_requested_line(app_page):
    original = ''.join(f'line {index}\r\n' for index in range(2100))
    edited = original.replace('line 1050\r\n','changed 1050\r\n')
    result = app_page.evaluate('([value,original])=>editorLineEndings(value,original)',
                               [edited.replace('\r\n','\n'),original])
    assert result == edited
    assert len(result.splitlines()) == 2100


def test_same_account_relogin_rechecks_project_read_before_restoring_private_views(app_page):
    page=app_page
    page.fill('#chat-compose','REVOKED_PROJECT_PRIVATE_DRAFT')
    page.evaluate("""() => {
      S.work.project='project-one';S.work.path='private.py';
      S.work.content='REVOKED_PROJECT_PRIVATE_SOURCE';S.work.original=S.work.content;
      S.work.console='REVOKED_PROJECT_PRIVATE_OUTPUT';S.work.dirty=true;
      const view=chatView();
      view.pending={receipt:'private-pending',text:'REVOKED_PROJECT_PRIVATE_REQUEST'};
      view.files.push({name:'private.png',preview:URL.createObjectURL(new Blob(['fixture']))});
    }""")
    expire(page)
    page.evaluate("fixtureProjects=[]")
    login(page)
    assert 'REVOKED_PROJECT_PRIVATE' not in page.locator('body').inner_text()
    assert page.evaluate('S.work.content')==''
    assert page.evaluate('S.work.console')==''
    assert not page.evaluate("""[...ChatUI.views.values()].some(v =>
      v.draft.includes('REVOKED_PROJECT_PRIVATE') ||
      v.pending?.text?.includes('REVOKED_PROJECT_PRIVATE') ||
      v.files.some(f=>f.name==='private.png'))""")
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_same_account_relogin_drops_native_cache_after_execute_revocation(app_page):
    page=app_page
    page.fill('#chat-compose','PRIVATE_NATIVE_EXECUTE_REVOKED')
    page.evaluate("S.work.project='project-one';S.work.content='readable editor draft'")
    expire(page)
    page.evaluate("fixtureNativeDenied=true")
    login(page)
    assert 'PRIVATE_NATIVE_EXECUTE_REVOKED' not in page.locator('body').inner_text()
    assert not page.evaluate("[...ChatUI.views.values()].some(v=>v.draft.includes('PRIVATE_NATIVE'))")
    assert page.evaluate('S.work.content')=='readable editor draft'
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_network_failure_keeps_suspended_draft_hidden_until_authority_recheck(app_page):
    page=app_page
    page.fill('#chat-compose','PRIVATE_NETWORK_SUSPENDED')
    expire(page)
    page.evaluate("""() => {
      window.recheckOffline=true;const original=api;
      api=async(path,options={})=>{
        if(recheckOffline && path.startsWith('/api/native/access?'))
          throw Object.assign(new Error('Fixture offline'),{status:503});
        return original(path,options);
      };
    }""")
    page.fill('#username','admin');page.fill('#password','fixture-password-only')
    page.locator('#login-form button[type=submit]').click()
    expect(page.locator('[data-action=retry-identity]')).to_be_visible()
    assert 'PRIVATE_NETWORK_SUSPENDED' not in page.locator('body').inner_text()
    assert page.evaluate("[...ChatUI.views.values()].some(v=>v.draft==='PRIVATE_NETWORK_SUSPENDED')")
    assert page.evaluate('!!S.suspendedUser')
    page.evaluate('recheckOffline=false')
    page.locator('[data-action=retry-identity]').click()
    expect(page.locator('#chat-compose')).to_have_value('PRIVATE_NETWORK_SUSPENDED')
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_upstream_401_keeps_real_native_draft_and_login(app_page):
    page = app_page
    page.fill('#chat-compose', 'Unsent draft survives unrelated upstream auth failure')
    assert page.evaluate("api('/api/fixture-resource-denied').catch(error=>error.code)") == 'UPSTREAM_AUTH_FAILED'
    expect(page.locator('#login-form')).to_have_count(0)
    expect(page.locator('#chat-compose')).to_have_value('Unsent draft survives unrelated upstream auth failure')
    assert page.evaluate("calls.filter(c=>c.path==='/api/session').length") == 1
    assert not page.evaluate("calls.some(c=>c.path==='/api/login'||c.path==='/api/logout')")


@pytest.mark.parametrize('native_allowed', [True, False])
def test_policy_recovery_rechecks_native_drafts_without_relogin(app_page, native_allowed):
    page = app_page
    page.fill('#chat-compose', 'Scoped native draft')
    page.evaluate('(allowed)=>{window.fixtureNativeDenied=!allowed;}', native_allowed)
    page.evaluate('recoverPanelAccess()')
    expect(page.locator('#login-form')).to_have_count(0)
    expect(page.locator('#chat-compose')).to_have_value('Scoped native draft' if native_allowed else '')
    assert page.evaluate('S.session.user_id') == 'owner-one'
    assert not page.evaluate("calls.some(c=>c.path==='/api/login'||c.path==='/api/logout'||c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_pending_validation_rejects_changed_form_before_any_second_request(app_page):
    page=app_page
    page.evaluate('''()=>{window.saveHandler=async(request,response)=>response({error:{
        code:'VALIDATION_PENDING',message:'fixture validation',operation_id:'d'.repeat(32)}},409)}''')
    open_project(page)
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form [role=status]')).to_contain_text('同一个保存键')
    page.fill('#workspace-form [name=subpath]','changed-source')
    page.click('button[type=submit][form=workspace-form]')
    expect(page.locator('#workspace-form [role=status]')).to_contain_text('原保存结果尚未核实')
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length")==1


def test_canonical_workspace_hash_navigation_replaces_inflight_root_read(app_page):
    page=app_page
    page.evaluate("""() => {
      stopEvents();S.page='spaces';S.workspacePath='spaces';renderShell();
      history.replaceState(null,'',location.pathname);
      const originalFetch=window.fetch;
      window.priorMutations=JSON.stringify(calls.filter(call=>call.method!=='GET'));
      window.workspaceReadCount=0;
      window.fetch=(path,options={})=>{
        if(path==='/api/spaces' && ++workspaceReadCount===1)
          return new Promise(resolve=>{window.releaseRootRead=()=>resolve(new Response(
            JSON.stringify({spaces:[{id:'stale-space',label:'Stale root read'}]}),
            {status:200,headers:{'Content-Type':'application/json'}}));});
        return originalFetch(path,options);
      };
      window.rootRender=renderPage();
    }""")
    page.wait_for_function('typeof releaseRootRead==="function"')
    # Hash-only navigation is how the OIDC callback's root page can become the
    # workspace page without creating another document or changing root S.page.
    page.evaluate("location.hash='spaces'")
    page.evaluate('releaseRootRead()')
    expect(page.locator('#workspace-page')).to_be_visible(timeout=10000)
    expect(page.locator('#workspace-page')).to_contain_text('Explicit workspace')
    expect(page.locator('#workspace-page')).not_to_contain_text('Stale root read')
    assert page.evaluate('workspaceReadCount')==2
    assert page.evaluate("JSON.stringify(calls.filter(call=>call.method!=='GET'))===priorMutations")
