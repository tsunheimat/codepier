"""Served panel flows on a real isolated Hub/Agent; no external accounts/hosts."""
import json
import uuid

import pytest
from playwright.sync_api import expect

from tests.test_integrations_stack import integrated_stack, resolved
from tests.identity_fixtures import team  # noqa: F401


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_sidebar_subtabs_follow_routes_and_page_tabs(stack, chat_browser_pool, tmp_path, engine, width, height):
    page = chat_browser_pool(engine).new_page(viewport={'width': width, 'height': height})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def sidebar_click(route):
        if width <= 900:
            page.locator('.mobile-menu').click()
        button = page.locator(f'.sidebar [data-nav="{route}"]')
        button.click()
        expect(button).to_have_attribute('aria-current', 'page')
        if width <= 900:
            expect(page.locator('.mobile-menu')).to_have_attribute('aria-expanded', 'false')

    def selected(area, tab):
        expect(page.locator(f'.sidebar [data-nav-subtabs="{area}"] .active')).to_have_attribute('data-nav-tab', tab)
        expect(page.locator('.sidebar [data-nav-tab].active')).to_have_count(1)
        expect(page.locator(f'.sidebar [data-nav="{area}"]')).to_have_class('active')

    try:
        page.goto(stack.url + '/#resources/projects')
        page.fill('#username', 'admin'); page.fill('#password', stack.password); page.click('#login-form button')
        expect(page.locator('#resources-page')).to_be_visible()
        selected('resources', 'projects')
        sidebar_click('resources/devices')
        expect(page.locator('#resources-page [data-nav="resources/devices"]')).to_have_attribute('aria-pressed', 'true')
        expect(page).to_have_url(stack.url + '/#resources/devices')
        page.locator('#resources-page [data-nav="resources/mcp"]').click()
        selected('resources', 'mcp')
        page.go_back()
        expect(page.locator('#resources-page [data-nav="resources/devices"]')).to_have_attribute('aria-pressed', 'true')
        selected('resources', 'devices')
        page.go_forward()
        expect(page.locator('#resources-page [data-nav="resources/mcp"]')).to_have_attribute('aria-pressed', 'true')
        selected('resources', 'mcp')
        sidebar_click('resources/vps')
        expect(page.get_by_role('heading',name='VPS 资源库',exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role('heading',name='VPS 资源库',exact=True)).to_be_visible()
        selected('resources', 'vps')
        sidebar_click('resources/projects')
        expect(page.locator('[data-project-detail]')).to_have_count(3)
        page.evaluate('window.sidebarRenderSeq=S.renderSeq;window.retainedCard=document.querySelector("[data-project-detail]")')
        sidebar_click('resources/projects')
        assert page.evaluate('S.renderSeq===sidebarRenderSeq && retainedCard.isConnected')
        sidebar_click('spaces')
        page.locator('a[href="#spaces/'+stack.space_id+'"]').click()
        for tab in ('profiles','connections','members','folders','shell'):
            link=page.locator('#workspace-page .product-tabs a[href="#spaces/'+stack.space_id+'/'+tab+'"]')
            link.click();expect(link).to_have_attribute('aria-current','page')
            expect(page).to_have_url(stack.url+'/#spaces/'+stack.space_id+'/'+tab)
        assert page.locator('#iam-active-space,[data-nav="access/roles"]').count()==0
        for tab in ('account', 'users', 'sso'):
            sidebar_click('identity/' + tab)
            expect(page.locator(f'[data-identity-tab={tab}]')).to_have_attribute('aria-pressed', 'true')
            selected('identity', tab)
        for tab in ('operations', 'sessions', 'events'):
            sidebar_click('audit/' + tab)
            expect(page.locator(f'#page [data-mode={tab}]')).to_have_class('active')
            selected('audit', tab)
        if width <= 900:
            page.locator('.mobile-menu').click()
        else:
            page.locator('.sidebar [data-nav="resources/projects"]').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'sidebar-subtabs-{engine}-{width}.png'), full_page=True)
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width,height', [(1440,1000),(390,844)])
def test_selected_tools_tab_does_not_replace_form_during_typing(integrated_stack,chat_browser_pool,engine,width,height):
    s=integrated_stack
    page=chat_browser_pool(engine).new_page(viewport={'width':width,'height':height})
    try:
        page.goto(s.url+'/#project/'+s.project['id']+'/tools?tab=validation')
        page.fill('#username','admin');page.fill('#password',s.password);page.click('#login-form button')
        expect(page.locator('[name=command]')).to_be_visible()
        page.evaluate('''() => {
          window.draftEvents=[];
          window.draftRenderSeq=S.renderSeq;
          window.holdDraftRefresh=true;
          window.draftSnapshotReads=[];
          window.draftField=document.querySelector('[name=command]');
          const read=api;
          window.api=async(path,options)=>{
            if(['/api/projects','/api/devices'].includes(path))draftSnapshotReads.push(path);
            const response=await read(path,options);
            if(path==='/api/projects' && holdDraftRefresh){
              holdDraftRefresh=false;
              await new Promise(resolve=>window.releaseDraftRefresh=resolve);
            }
            return response;
          };
          draftField.addEventListener('focus',()=>{
            draftEvents.push({event:'focus',connected:draftField.isConnected});
            if(window.releaseDraftRefresh)releaseDraftRefresh();
          });
          document.addEventListener('input',event=>{
            if(event.target.name==='command')draftEvents.push({event:'input',value:event.target.value});
          },true);
        }''')
        page.locator('[data-i-tab=validation]').click()
        if page.evaluate('S.renderSeq!==draftRenderSeq'):
            page.wait_for_function('() => typeof releaseDraftRefresh === "function"')
        # The selected tab should keep the editable form mounted. A refresh at
        # this focus/text boundary can otherwise direct input to the tab itself.
        page.locator('[name=command]').fill('printf retained-navigation-draft')
        print('DRAFT_TYPING='+json.dumps(page.evaluate('({events:draftEvents,connected:draftField.isConnected,active:document.activeElement?.id,value:document.querySelector("[name=command]")?.value,drafts:S.integrations.drafts})')))
        expect(page.locator('[name=command]')).to_have_value('printf retained-navigation-draft')
        assert page.evaluate('draftField.isConnected')
        assert page.evaluate('S.renderSeq===draftRenderSeq')
        assert not page.evaluate('draftSnapshotReads')
        page.evaluate('holdDraftRefresh=false;window.releaseDraftRefresh?.()')
        page.evaluate("navigate('resources/projects')")
        page.evaluate('(project)=>CodePierIntegrations.open({project,tab:"validation"})',s.project['id'])
        expect(page.locator('[name=command]')).to_have_value('printf retained-navigation-draft')
    finally:page.close()


@pytest.mark.parametrize('width,height', [(1440,1000),(390,844)])
def test_resources_devices_project_tools_artifacts_and_access_members(integrated_stack,chat_browser_pool,tmp_path,width,height):
    s=integrated_stack; page=chat_browser_pool('chromium').new_page(viewport={'width':width,'height':height})
    errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(s.url+'/#resources/devices');page.fill('#username','admin');page.fill('#password',s.password);page.click('#login-form button')
        expect(page.locator('#resources-page')).to_be_visible()
        expect(page.locator('[data-device-state]')).to_have_count(1)
        assert page.evaluate('S.page')=='resources' and page.evaluate('S.resourceTab')=='devices'
        assert not page.locator('.sidebar [data-nav=devices],.sidebar [data-nav=integrations],.sidebar [data-nav=artifacts],.sidebar [data-nav=members]').count()
        node=page.locator('[data-device-state]').first
        node.locator('[data-action=device-detail]').click()
        expect(page.locator('.modal-body')).to_contain_text(s.device)
        page.locator('.modal [data-action=close-modal]').first.click()
        page.locator('#resources-page [data-nav="resources/projects"]').click()
        # Resource details carry this exact project, even when the workbench
        # currently points at another project.
        ws=resolved(s,'worktrees_create',{'label':'Navigation '+uuid.uuid4().hex[:8]},panel=True)
        page.evaluate('(other)=>{S.work.project=other;S.work.workspace_id="";}',s.projects[1]['id'])
        page.evaluate('(args)=>CodePierProduct.resourceDetail("project",args.project,args.workspace)',
                      {'project':s.project['id'],'workspace':ws['workspace_id']})
        page.locator('[data-ws-project-tools]').click()
        expect(page.locator('#integration-center')).to_be_visible()
        expect(page.locator('#i-project')).to_have_value(s.project['id'])
        # Project details start from the one bound source, not an ambient worktree.
        expect(page.locator('#i-workspace')).to_have_value('')
        page.evaluate('(id)=>location.hash="project/"+id+"/tools?workspace_id=&tab=overview"',s.projects[1]['id'])
        expect(page.locator('#i-project')).to_have_value(s.projects[1]['id'])
        expect(page.locator('#i-workspace')).to_have_value('')
        page.evaluate('(args)=>CodePierIntegrations.open(args)',{'project':s.project['id'],'workspace_id':ws['workspace_id'],'tab':'validation'})
        assert 'WORKFLOW' not in page.locator('#page').inner_text()
        assert '/tools?' in page.url and ws['workspace_id'] in page.url
        page.locator('[data-i-tab=validation]').click()
        page.locator('[name=command]').fill('printf retained-navigation-draft')
        page.evaluate("navigate('resources/projects')")
        page.evaluate('(args)=>CodePierIntegrations.open(args)',{'project':s.project['id'],'workspace_id':ws['workspace_id'],'tab':'validation'})
        expect(page.locator('[name=command]')).to_have_value('printf retained-navigation-draft')
        page.evaluate('(project)=>CodePierIntegrations.open({project,tab:"validation"})',s.project['id'])
        expect(page.locator('#i-workspace')).to_have_value('')
        expect(page.locator('[name=command]')).to_have_value('')
        page.evaluate('(project)=>CodePierIntegrations.open({project,tab:"validation"})',s.projects[1]['id'])
        expect(page.locator('[name=command]')).to_have_value('')
        page.evaluate('(args)=>CodePierIntegrations.open(args)',{'project':s.project['id'],'workspace_id':ws['workspace_id'],'tab':'validation'})
        expect(page.locator('[name=command]')).to_have_value('printf retained-navigation-draft')
        page.reload();expect(page.locator('#i-project')).to_have_value(s.project['id'])
        expect(page.locator('#i-workspace')).to_have_value(ws['workspace_id'])
        # Existing file snapshots remain separate from the conversation index.
        filename='download-'+uuid.uuid4().hex[:8]+'.txt';body='immutable fixture '+uuid.uuid4().hex
        (s.imago/filename).write_text(body)
        other=resolved(s,'artifacts_register',{'project':s.projects[1]['id'],'path':'README.md'},panel=True)
        prior=s.call('artifacts_list',{'project':s.project['id']})['artifacts']
        total_before=len(s.call('artifacts_list',{})['artifacts'])
        page.evaluate("navigate('resources/projects')")
        page.locator('[data-project-detail="'+s.project['id']+'"]').click()
        page.locator('[data-ws-project-artifacts]').click()
        expect(page.locator('#artifact-project')).to_have_value(s.project['id'])
        assert '/artifacts' in page.url
        page.locator('[data-insight=register]').click()
        page.locator('#artifact-form [name=path]').fill(filename)
        page.locator('#artifact-save').click()
        expect(page.locator('#artifact-form')).to_have_count(0,timeout=20000)
        expect(page.locator('.modal')).to_contain_text(filename)
        link=page.locator('.modal a[download]')
        href=link.get_attribute('href');assert 'space_id=' not in href
        with page.expect_download() as downloaded:link.click()
        assert open(downloaded.value.path()).read()==body
        page.locator('.modal [data-action=close-modal]').first.click()
        expect(page.locator('.artifact-card')).to_have_count(len(prior)+1)
        assert other['artifact_id'] not in page.locator('#page').inner_text()
        page.reload();expect(page.locator('#artifact-project')).to_have_value(s.project['id'])
        page.evaluate('(id)=>location.hash="project/"+id+"/artifacts"',s.projects[1]['id'])
        expect(page.locator('#artifact-project')).to_have_value(s.projects[1]['id'])
        page.locator('[data-artifact-overview]').click();expect(page.locator('.artifact-card')).to_have_count(total_before+1)
        page.evaluate('route=>navigate(route)','spaces/'+s.space_id+'/members')
        expect(page.locator('#workspace-page')).to_be_visible()
        page.locator('[data-ws-action=invite]').click()
        expect(page.locator('#workspace-form [data-policy-kind]:checked')).to_have_count(0)
        page.get_by_role('button',name='保存',exact=True).click()
        expect(page.locator('[data-copy-invitation]')).to_be_visible()
        assert page.evaluate('S.page')=='spaces' and page.evaluate('S.space_id === undefined')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'navigation-{width}.png'),full_page=True)
        assert not errors,errors
    finally:page.close()


def test_inaccessible_project_deep_link_does_not_select_another_project(stack,chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    try:
        page.goto(stack.url+'/#project/not-authorized/tools?workspace_id=')
        page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#page')).to_contain_text('无权访问')
        expect(page.locator('#integration-center')).to_have_count(0)
        page.goto(stack.url+'/#project/not-authorized/artifacts')
        expect(page.locator('#page')).to_contain_text('无权访问')
        expect(page.locator('.artifact-card')).to_have_count(0)
    finally:page.close()


def test_tools_refresh_after_real_project_unmapping_keeps_context_and_draft(stack,chat_browser_pool):
    page=chat_browser_pool('chromium').new_page();calls=[]
    # Simulate an unavailable event stream: all HTTP permission checks remain
    # real. An actual entitlement-invalidating SSE reconnect may end the session.
    page.add_init_script('window.EventSource=class extends EventTarget{close(){}};')
    def route(request_route):
        calls.append(request_route.request.post_data_json)
        request_route.continue_()
    try:
        page.route('**/api/tools/call',route)
        page.goto(stack.url+'/#project/'+stack.project['id']+'/tools?tab=validation')
        page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#i-project')).to_have_value(stack.project['id'])
        page.locator('[name=command]').fill('printf keep-original-project-draft')
        assert stack.client.delete('/api/projects/'+stack.project['id'],params={'expected_version':stack.project['version']}).status_code==200
        page.locator('.topbar [data-action=refresh]').click()
        expect(page.locator('#page')).to_contain_text('无权访问')
        expect(page.locator('#integration-center')).to_have_count(0)
        assert page.evaluate('S.integrations.project')==stack.project['id']
        assert 'keep-original-project-draft' in page.evaluate('JSON.stringify(S.integrations.drafts)')
        assert stack.project['id'] in page.url
        assert len(stack.client.get('/api/projects').json()['projects'])==2
        assert not any(c['tool'] in {'validation_run','shell_exec'} for c in calls)
    finally:page.unroute_all(behavior='wait');page.close()


def test_artifact_unknown_reply_recovers_original_receipt_after_navigation_and_reload(integrated_stack,chat_browser_pool):
    s=integrated_stack;page=chat_browser_pool('chromium').new_page()
    filename='recover-'+uuid.uuid4().hex[:8]+'.txt';(s.imago/filename).write_text('Original snapshot')
    registrations=[]
    def route(request_route):
        body=request_route.request.post_data_json
        if body.get('tool')=='artifacts_register':
            registrations.append(body)
            response=request_route.fetch()
            assert response.ok
            request_route.abort('failed')  # Accepted request; only its reply is lost.
        else:request_route.continue_()
    try:
        page.goto(s.url+'/#project/'+s.project['id']+'/artifacts')
        page.fill('#username','admin');page.fill('#password',s.password);page.click('#login-form button')
        expect(page.locator('#artifact-project')).to_have_value(s.project['id'])
        page.route('**/api/tools/call',route)
        page.locator('[data-insight=register]').click();page.locator('#artifact-form [name=path]').fill(filename)
        page.locator('#artifact-save').click()
        expect(page.locator('[data-operation-hint]')).to_contain_text('原操作回执待核实',timeout=15000)
        attempts=len(registrations)
        page.once('dialog',lambda dialog:dialog.accept())
        page.evaluate('(project)=>CodePierIntegrations.open({project,tab:"overview"})',s.project['id'])
        expect(page.locator('#i-receipts')).to_contain_text('文件快照')
        page.reload();expect(page.locator('#i-receipts')).to_contain_text('文件快照')
        page.locator('#i-receipts').get_by_role('button',name='查询原结果').click()
        expect(page.locator('#i-result')).to_contain_text(filename,timeout=20000)
        assert registrations and all(body==registrations[0] for body in registrations)
        assert len(registrations)==attempts
        # The transport may retry its fixed idempotency key. Navigation/reload
        # recovery only queries the receipt and dispatches no additional request.
        assert len([a for a in s.call('artifacts_list',{'project':s.project['id']})['artifacts'] if a['name']==filename])==1
        expect(page.locator('#i-receipts .integration-receipt')).to_have_count(0)
    finally:page.unroute_all(behavior='wait');page.close()


def test_existing_project_search_opens_paged_current_source_without_workflow(integrated_stack,chat_browser_pool):
    s=integrated_stack;page=chat_browser_pool('chromium').new_page()
    filename='source-'+uuid.uuid4().hex[:8]+'.py'
    (s.imago/filename).write_text('def navigation_fixture():\n    return "scoped_source_marker"\n'+''.join('# row '+str(n)+'\n' for n in range(450)))
    try:
        page.goto(s.url+'/#workbench');page.fill('#username','admin');page.fill('#password',s.password);page.click('#login-form button')
        expect(page.locator('#work-project')).to_be_visible();page.locator('#work-project').select_option(s.project['id'])
        page.locator('[data-insight=search]').click()
        page.locator('#session-search-form [name=query]').fill('scoped_source_marker')
        page.locator('#session-search-form [name=file_glob]').fill(filename)
        page.locator('#session-search-start').click()
        expect(page.locator('.search-results [data-insight=source]')).to_be_visible(timeout=20000)
        page.locator('.search-results [data-insight=source]').click()
        expect(page.locator('.modal .code-box')).to_contain_text('scoped_source_marker')
        expect(page.locator('.modal-body')).to_contain_text('第 2–401 行')
        page.locator('.modal-footer [data-insight=source]').click()
        expect(page.locator('.modal-body')).to_contain_text('第 402–452 行')
        assert 'scoped_source_marker' not in page.locator('.modal .code-box').inner_text()
        assert 'default_space_id' not in s.client.get('/api/iam/me').json()
    finally:page.close()


@pytest.mark.parametrize('bootstrap',[True,False])
def test_fresh_oidc_onboarding_matches_configured_first_login(tmp_path,monkeypatch,chat_browser_pool,bootstrap):
    from fastapi.testclient import TestClient
    from urllib.parse import urlsplit
    from tests.test_oidc_bootstrap import Provider,fresh_app,seed_env
    fake=Provider();seed_env(monkeypatch,fake,CODEPIER_OIDC_BOOTSTRAP_ADMIN='first-login' if bootstrap else None)
    app=fresh_app(tmp_path/'fresh-oidc',fake)
    with TestClient(app) as client:
        page=chat_browser_pool('chromium').new_page()
        def route(request_route):
            req=request_route.request
            if urlsplit(req.url).hostname!='127.0.0.1':request_route.abort();return
            response=client.request(req.method,req.url,headers=req.all_headers(),content=req.post_data_buffer,follow_redirects=False)
            headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}}
            request_route.fulfill(status=response.status_code,headers=headers,body=response.content)
        page.route('**/*',route)
        try:
            page.goto('http://127.0.0.1:8765/')
            expect(page.get_by_role('link',name='使用 Company SSO 登录')).to_be_visible()
            if bootstrap:
                expect(page.locator('#login-bootstrap-state')).to_contain_text('首次 SSO 管理员初始化')
                assert 'python -m hub init' not in page.locator('#login-bootstrap-state').inner_text()
                expect(page.locator('#oidc-login-buttons')).to_contain_text('自己的 Personal Space')
            else:
                expect(page.locator('#login-bootstrap-state')).to_contain_text('python -m hub init')
            assert not app.state.store.one("SELECT 1 FROM spaces WHERE id='legacy'")
        finally:page.close()


def test_members_entry_is_admin_only_and_membership_assignment_gates_remain_real(team,chat_browser_pool):
    from urllib.parse import urlsplit
    app,b=team
    page=chat_browser_pool('chromium').new_page()
    page.add_init_script("window.EventSource=class extends EventTarget{close(){}};")
    def route(request_route):
        req=request_route.request
        if urlsplit(req.url).hostname!='127.0.0.1':request_route.abort();return
        response=b['alice'].client.request(req.method,req.url,headers=req.all_headers(),content=req.post_data_buffer,follow_redirects=False)
        request_route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}},body=response.content)
    page.route('**/*',route)
    try:
        page.goto('http://127.0.0.1:8765/#spaces/team/members')
        page.fill('#username','alice');page.fill('#password','fixture-password-only');page.click('#login-form button')
        expect(page.locator('#page')).to_contain_text('没有提供该管理页面')
        assert not page.locator('[data-product-area=access][data-product-tab=members]').count()
        assert not page.locator('.sidebar [data-nav="access/members"]').count()
        assert not page.locator('[data-iam=invite],[data-iam=assign-role]').count()
        assert b['alice'].get('/api/spaces/team/members').status_code==403
        assert b['alice'].post('/api/spaces/team/invites',json={'policy':{'manage':['workspace']},'days':1}).status_code==403
    finally:page.close()


@pytest.mark.parametrize('saved',['team','legacy','own_personal','other_personal'])
def test_old_saved_selection_is_ignored_not_used_as_authority(team,chat_browser_pool,saved):
    from urllib.parse import urlsplit
    app,b=team;store=app.state.store
    personal=store.one("SELECT id FROM spaces WHERE owner_user_id='owner' AND kind='personal'")['id']
    other=store.one("SELECT id FROM spaces WHERE owner_user_id='alice' AND kind='personal'")['id']
    selected={'team':'team','legacy':'legacy','own_personal':personal,'other_personal':other}[saved]
    page=chat_browser_pool('chromium').new_page()
    page.add_init_script('sessionStorage.setItem("codepier-space:owner",'+json.dumps(selected)+');window.EventSource=class extends EventTarget{close(){}};')
    def route(request_route):
        req=request_route.request
        if urlsplit(req.url).hostname!='127.0.0.1':request_route.abort();return
        response=b['owner'].client.request(req.method,req.url,headers=req.all_headers(),content=req.post_data_buffer,follow_redirects=False)
        request_route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}},body=response.content)
    page.route('**/*',route)
    try:
        page.goto('http://127.0.0.1:8765/#resources/projects')
        page.fill('#username','owner');page.fill('#password','fixture-password-only');page.click('#login-form button')
        expect(page.locator('#resources-page')).to_be_visible()
        expect(page.locator('#iam-active-space')).to_have_count(0)
        assert page.evaluate('S.space_id === undefined')
        page.reload();expect(page.locator('#resources-page')).to_be_visible()
        expect(page.locator('#iam-active-space')).to_have_count(0)
        assert b['owner'].get('/api/projects',headers={'X-CodePier-Space':other}).status_code==400
        assert other not in b['owner'].get('/api/spaces/'+other+'/projects').text
    finally:page.close()
