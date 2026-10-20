"""Shipped workspace renderer and real isolated Hub/Runner end-to-end flows."""
import base64
import hashlib
import json
import re
import uuid
from urllib.parse import parse_qs,urlsplit

import pytest
from playwright.sync_api import expect


@pytest.fixture
def product_page(stack,chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    page.goto(stack.url+'/#spaces/'+stack.space_id+'/projects')
    page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#workspace-page')).to_be_visible(timeout=15000)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:yield page,errors
    finally:page.close()


def navigate(page,stack,tab):
    page.evaluate('(route)=>navigate(route)','spaces/'+stack.space_id+'/'+tab)
    expect(page).to_have_url(stack.url+'/#spaces/'+stack.space_id+'/'+tab)
    expect(page.locator('#workspace-page')).to_be_visible()


@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_workspace_project_profile_connection_and_audit_flow(stack,product_page,tmp_path,width,height):
    page,errors=product_page;page.set_viewport_size({'width':width,'height':height})
    unique=uuid.uuid4().hex[:8];project_name='Resource-'+unique
    assert page.locator('#iam-active-space,#roles-page,#role-create').count()==0
    source=stack.root/('resource-'+unique);source.mkdir()
    page.locator('[data-ws-action="new-projects"]').click()
    form=page.locator('#workspace-form')
    form.locator('[name="alias"]').fill(project_name)
    form.locator('[name="folder_id"]').select_option(stack.folder['id'])
    form.locator('[name="subpath"]').fill(source.name)
    expect(form.locator('[name="root"],[name="device_id"]')).to_have_count(0)
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form).to_have_count(0,timeout=60000)
    expect(page.locator('#workspace-page h3').filter(has_text=project_name)).to_be_visible()
    project=next(p for p in stack.client.get('/api/projects').json()['projects'] if p['alias']==project_name)
    assert project['folder_id']==stack.folder['id'] and project['subpath']==source.name
    navigate(page,stack,'profiles')
    page.locator('[data-ws-action="new-profiles"]').click()
    form.locator('[name="label"]').fill('Worker '+unique)
    for action in ('read','write','execute'):
        form.locator('[data-policy-kind="projects"][data-resource="'+stack.project['id']+'"][value="'+action+'"]').check()
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form).to_have_count(0,timeout=15000)
    profile=next(p for p in stack.client.get(f'/api/spaces/{stack.space_id}/profiles').json()['profiles'] if p['label']=='Worker '+unique)
    navigate(page,stack,'connections')
    page.locator('[data-ws-action="new-connections"]').click()
    form.locator('[name="name"]').fill('Client '+unique)
    form.locator('[name="profile_id"]').select_option(profile['id'])
    expect(form.locator('[name="role_id"]')).to_have_count(0)
    expect(form.locator('[data-profile-summary]')).to_contain_text('execute')
    page.get_by_role('button',name='保存',exact=True).click()
    expect(page.locator('[data-copy-credential]')).to_be_visible(timeout=15000)
    token=page.locator('.modal .secret').inner_text()
    assert token not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
    page.get_by_role('button',name='完成',exact=True).click()
    navigate(page,stack,'connections')
    expect(page.locator('#workspace-page')).to_contain_text('固定同意范围')
    actual=stack.mcp('get_access_context',token_value=token)['structuredContent']
    assert actual['profile']['id']==profile['id'] and actual['connection']['id']!=profile['id']
    response=stack.must(stack.rpc('tools/call',{'name':'exec','arguments':{
        'project':'Imago','command':'printf product-browser-fixture','yield_seconds':0,
        'idempotency_key':uuid.uuid4().hex},'_meta':{'openai/session':'browser-'+unique}},token_value=token))['result']
    assert not response.get('isError'),response
    identifier=response['structuredContent']['operation_id']
    assert stack.poll(identifier)['result']['data']['output']=='product-browser-fixture'
    page.evaluate('navigate("audit/sessions")')
    expect(page.locator('#audit-sessions')).to_be_visible()
    row=next(r for r in stack.client.get('/api/conversations').json()['conversations'] if r['conversation_identifier']=='browser-'+unique)
    page.locator('[data-session-id="'+row['id']+'"] a',has_text='查看会话时间线').click()
    entry=page.locator('[data-call-id="'+identifier+'"]')
    expect(entry).to_contain_text('Imago');expect(entry).to_contain_text(identifier[:12])
    edited=stack.client.put('/api/conversations/'+row['id'],json={
        'platform':'chatgpt','conversation_identifier':'browser-'+unique,
        'original_url':'https://chatgpt.com/c/supplied-'+unique})
    assert edited.status_code==200,edited.text
    page.reload();expect(entry).to_be_visible()
    page.get_by_text('关联证据',exact=True).click()
    expect(page.get_by_role('link',name='原对话网址（可选资料）')).to_have_attribute('href','https://chatgpt.com/c/supplied-'+unique)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path/f'workspace-product-{width}.png'),full_page=True)
    assert errors==[]


@pytest.mark.parametrize('route',['workflows','access/roles','conversations/archive'])
def test_removed_navigation_has_no_role_workflow_or_archive_actions(stack,chat_browser_pool,route):
    page=chat_browser_pool('chromium').new_page();writes=[]
    page.on('request',lambda r:writes.append((r.url,r.post_data or '')) if r.method=='POST' else None)
    try:
        page.goto(stack.url+'/#'+route);page.fill('#username','admin');page.fill('#password',stack.password)
        page.click('#login-form button')
        page.evaluate('navigate("spaces")')
        expect(page.locator('#workspace-page')).to_be_visible(timeout=15000)
        expect(page.locator('#role-form,#role-create,#conversation-create,[data-archive-detail]')).to_have_count(0)
        assert not any('/api/access-roles' in url or 'workflows_' in body for url,body in writes)
        page.reload();expect(page.locator('#workspace-page')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    finally:page.close()


def test_project_detail_preserves_click_intent_during_passive_refresh(stack,product_page):
    page,errors=product_page;held=[];path='/api/projects/'+stack.project['id']+'/vps'
    page.route('**'+path,lambda route:held.append(route))
    try:
        with page.expect_request('**'+path):
            page.locator('[data-project-detail="'+stack.project['id']+'"]').click()
        page.evaluate('renderPage(false)')
        assert held
        held.pop().fulfill(json=stack.must(stack.client.get(path)))
        expect(page.locator('#modal-title')).to_contain_text('Imago')
        expect(page.locator('.modal-body')).to_contain_text('工作空间')
        expect(page.locator('[data-ws-project-shell]')).to_be_visible()
        assert errors==[]
    finally:
        for route in held:route.abort()
        page.unroute_all(behavior='wait')


def test_oauth_space_profile_and_live_confirmation_never_create_implicit_identity(stack,product_page):
    page,errors=product_page
    profile=stack.create_profile({'project_rules':[{'projects':[stack.project['id']],'actions':['read']}]})
    registered=stack.must(stack.client.post('/oauth/register',json={'redirect_uris':['http://localhost:12345/callback']}))
    verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    pending=stack.client.get('/oauth/authorize',params={'response_type':'code','client_id':registered['client_id'],
        'redirect_uri':'http://localhost:12345/callback','scope':'codepier.workspace','code_challenge_method':'S256',
        'code_challenge':challenge,'resource':stack.url+'/mcp'},follow_redirects=False)
    assert pending.status_code==303,pending.text
    page.route('http://localhost:12345/callback*',lambda route:route.fulfill(body='Isolated callback'))
    path=f'/api/spaces/{stack.space_id}/connections'
    before=stack.client.get(path).json()['connections']
    page.goto(stack.url+pending.headers['location'])
    expect(page.locator('[name="consent_profile"]')).to_be_disabled()
    expect(page.locator('[data-consent-allow]')).to_be_disabled()
    page.select_option('[name="consent_space"]',stack.space_id)
    page.select_option('[name="consent_profile"]',profile['id'])
    page.select_option('[name="consent_mode"]','live')
    confirmation=page.locator('[name="confirm_live_profile"]')
    expect(confirmation).not_to_be_checked()
    page.locator('[data-consent-allow]').click()
    assert stack.client.get(path).json()['connections']==before
    confirmation.check()
    page.locator('[data-consent-allow]').click()
    expect(page).to_have_url(re.compile(r'http://localhost:12345/callback\?'),timeout=15000)
    code=parse_qs(urlsplit(page.url).query)['code'][0]
    tokens=stack.must(stack.client.post('/oauth/token',data={'grant_type':'authorization_code','code':code,
        'client_id':registered['client_id'],'redirect_uri':'http://localhost:12345/callback',
        'resource':stack.url+'/mcp','code_verifier':verifier}))
    context=stack.mcp('get_access_context',token_value=tokens['access_token'])['structuredContent']
    assert context['profile']['id']==profile['id'] and context['connection']['id']!=profile['id']
    assert context['scopes']==['read'] and 'role' not in context
    assert len(stack.client.get(path).json()['connections'])==len(before)+1
    assert errors==[]
