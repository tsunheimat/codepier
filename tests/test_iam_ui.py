"""Chromium/WebKit direct-workspace UI, real isolated Hub and Runner processes."""
from contextlib import closing
import os
import uuid

import httpx
import pytest
from playwright.sync_api import sync_playwright,expect
from hub.store import Store
from shared.crypto import password_hash
from tests.current_model import CurrentModel


@pytest.fixture(scope='module',params=['chromium','webkit'])
def iam_browser(request):
    with sync_playwright() as pw:
        executable=os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param=='chromium' else None
        browser=getattr(pw,request.param).launch(**({'executable_path':executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width':1440,'height':1000},{'width':390,'height':844}])
def iam_page(iam_browser,request,stack):
    context=iam_browser.new_context(viewport=request.param)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda err:errors.append(str(err)))
    page.goto(stack.url+'/#identity');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('账号与身份',timeout=15000)
    try:yield page,errors
    finally:
        context.close()
    assert not errors,errors


def test_workspace_creation_and_two_explicit_tabs_are_independent(iam_page,stack):
    page,_=iam_page
    page.get_by_role('button',name='管理工作空间',exact=True).click()
    page.locator('[data-ws-action="new-space"]').click()
    label='Team '+uuid.uuid4().hex[:8]
    page.locator('#workspace-form [name="label"]').fill(label)
    page.get_by_role('button',name='保存',exact=True).click()
    expect(page.locator('#workspace-form')).to_have_count(0,timeout=15000)
    expect(page.locator('#workspace-page h1')).to_have_text(label)
    spaces=stack.must(stack.client.get('/api/spaces'))['spaces']
    sid=next(s['id'] for s in spaces if s['label']==label)
    assert page.url.endswith('/#spaces/'+sid)
    second=page.context.new_page();second.goto(stack.url+'/#spaces/'+stack.space_id+'/projects')
    try:
        expect(second.locator('[data-project-detail]')).to_have_count(3,timeout=15000)
        page.locator('a[href="#spaces/'+sid+'/projects"]').click()
        expect(page.locator('#workspace-page h2')).to_have_text('Projects')
        expect(page.locator('[data-project-detail]')).to_have_count(0)
        expect(second.locator('[data-project-detail]')).to_have_count(3)
        assert second.url.endswith('/#spaces/'+stack.space_id+'/projects')
        assert page.evaluate('S.space_id === undefined') and second.evaluate('S.space_id === undefined')
        assert not {'current_space_id','active_space_id','default_space_id'} & stack.must(stack.client.get('/api/iam/me')).keys()
        page.reload();expect(page.locator('#workspace-page h2')).to_have_text('Projects')
        expect(page.locator('[data-project-detail]')).to_have_count(0)
    finally:second.close()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_provider_form_is_closed_by_default_and_does_not_echo_secret(iam_page,stack):
    page,_=iam_page
    page.evaluate("navigate('identity/sso')")
    page.locator('[data-iam="new-provider"]').click()
    form=page.locator('#iam-form')
    expect(form.locator('[name="enabled"]')).not_to_be_checked()
    expect(form.locator('[name="admission"]')).to_have_value('closed')
    expect(form).to_contain_text('Microsoft Entra');expect(form).to_contain_text('UserInfo')
    identifier=uuid.uuid4().hex[:8];label='Test SSO '+identifier
    for name,value in [('label',label),('issuer','https://idp-'+identifier+'.example.invalid/'),('client_id','client-id'),('client_secret','test-ui-secret-not-a-real-credential')]:
        form.locator('[name="'+name+'"]').fill(value)
    page.locator('button[form="iam-form"]').click()
    expect(form).to_have_count(0)
    row=page.locator('.grant-row').filter(has_text=label);expect(row).to_be_visible()
    row.locator('[data-iam-provider]').click()
    expect(page.locator('#iam-form [name="client_secret"]')).to_have_value('')
    expect(page.locator('#iam-form [name="issuer"]')).to_have_attribute('readonly','')
    assert 'test-ui-secret-not-a-real-credential' not in page.content()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_invitation_direct_permissions_acceptance_and_revocation_ui(iam_page,stack):
    page,_=iam_page;space=stack.space_id
    page.evaluate('(route)=>navigate(route)','spaces/'+space+'/members')
    expect(page.locator('#workspace-page h2')).to_have_text('成员与权限')
    page.on('dialog',lambda dialog:dialog.accept())
    def create():
        page.locator('[data-ws-action="invite"]').click()
        form=page.locator('#workspace-form')
        expect(form.locator('[name="level"],[name="role_id"]')).to_have_count(0)
        expect(form.locator('[data-policy-kind]:checked')).to_have_count(0)
        form.locator('[data-policy-kind="projects"][data-resource="'+stack.project['id']+'"][value="read"]').check()
        page.get_by_role('button',name='保存',exact=True).click()
        expect(page.locator('[data-copy-invitation]')).to_be_visible(timeout=15000)
        secret=page.locator('.modal .secret').inner_text();assert secret.startswith('cpi_')
        assert secret not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
        page.get_by_role('button',name='完成',exact=True).click()
        page.evaluate('renderPage(false)')
        expect(page.locator('[data-ws-invite]')).to_have_count(1)
        assert secret not in page.content()
        return secret
    secret=create()
    uid='invitee-'+uuid.uuid4().hex
    with closing(Store(stack.hubdir)) as store:
        CurrentModel(store).user(uid,password_hash=password_hash('isolated-invitation-password'))
    with httpx.Client(base_url=stack.url,timeout=20,trust_env=False) as client:
        login=stack.must(client.post('/api/login',json={'username':uid,'password':'isolated-invitation-password'},headers={'Origin':stack.url}))
        client.headers.update({'X-RD-CSRF':login['csrf'],'Origin':stack.url})
        assert client.get('/api/spaces/'+space+'/members').status_code==403
        accepted=stack.must(client.post('/api/iam/invites/accept',json={'invitation':secret}))
        assert accepted['space_id']==space
        visible=stack.must(client.get('/api/projects'))['projects']
        assert [p['id'] for p in visible]==[stack.project['id']]
        assert client.get('/api/spaces/'+space+'/members').status_code==403
        assert client.post('/api/iam/invites/accept',json={'invitation':secret}).status_code==400
        page.evaluate('renderPage(false)');expect(page.locator('[data-ws-invite]')).to_have_count(0)
        revoked=create()
        page.locator('[data-ws-invite]').click()
        expect(page.locator('[data-ws-invite]')).to_have_count(0)
        rejected=client.post('/api/iam/invites/accept',json={'invitation':revoked})
        assert rejected.status_code==400 and rejected.json()['error']['code']=='INVITATION_INVALID'
        # Revoking a different invitation does not revoke an accepted membership.
        assert [p['id'] for p in stack.must(client.get('/api/projects'))['projects']]==[stack.project['id']]
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_account_session_revocation_does_not_change_other_connections(iam_page,stack):
    page,_=iam_page
    assert page.locator('[data-iam-session]').count()>=1
    page.locator('.grant-row').filter(has_text='当前浏览器').locator('[data-iam-session]').click()
    expect(page.locator('#login-form')).to_be_visible()
    expect(page.locator('#page')).to_have_count(0)
    assert stack.client.get('/api/session').json()['authenticated']
