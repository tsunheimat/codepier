"""The real served renderer and isolated Hub/Agent; no host or provider account."""
import json
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub.store import Store
from hub.gateway.catalog import fingerprint
from tests.test_roles import role, must


@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_resources_role_connection_conversation_flow(stack,chat_browser_pool,tmp_path,width,height):
    page=chat_browser_pool('chromium').new_page(viewport={'width':width,'height':height})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    unique=uuid.uuid4().hex[:8]
    try:
        page.goto(stack.url+'/#projects');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#resources-page')).to_be_visible()
        assert page.evaluate('S.page')=='resources'
        assert page.locator('.sidebar .nav [data-nav=resources]').count()==1
        assert not page.locator('.sidebar .nav [data-nav=projects],.sidebar .nav [data-nav=profiles],.sidebar .nav [data-nav=workflows]').count()
        # Type-specific project configuration still validates through the fixture Agent.
        root=stack.imago/('resource-'+unique);root.mkdir()
        page.locator('[data-action=add-project]').click()
        page.locator('#project-form [name=alias]').fill('Resource-'+unique)
        page.locator('#project-form [name=root]').fill(str(root))
        page.locator('#project-form [name=device_id]').select_option(stack.device)
        page.click('#save-project')
        page.wait_for_function("() => !document.querySelector('#project-form') || !document.querySelector('#project-form [role=status]').textContent.includes('正在验证')")
        if page.locator('#project-form').count():
            status=page.locator('#project-form [role=status]').inner_text()
            assert '目录验证仍在进行' in status,status
            import re
            identifier=re.search(r'操作 ([a-f0-9]{32})',status).group(1)
            stack.poll(identifier,timeout=20)
            page.click('#save-project')
        expect(page.locator('#project-form')).to_have_count(0,timeout=10000)
        expect(page.locator('[data-project-row]').filter(has_text='Resource-'+unique)).to_be_visible()
        page.locator('[data-product-area=resources][data-product-tab=vps]').click()
        expect(page.locator('#vps-query')).to_be_visible()
        page.locator('[data-vps-action=add]').first.click()
        f=page.locator('#vps-form');f.locator('[name=name]').fill('Testing '+unique)
        f.locator('[name=host]').fill('testing-'+unique+'.invalid');f.locator('[name=password]').fill('FIXTURE_SSH_PASSWORD')
        f.locator('[name=execution_project_id]').select_option(stack.project['id'])
        f.locator('[name=project_ids][value="'+stack.project['id']+'"]').check()
        page.click('#vps-save');expect(f).to_have_count(0)
        card=page.locator('[data-vps-card]').filter(has_text='Testing '+unique)
        card.locator('[data-resource-detail]').click()
        expect(page.locator('.modal-body')).to_contain_text('SSH 账号权限')
        expect(page.locator('.modal-body')).to_contain_text('Imago')
        page.locator('.modal [data-action=close-modal]').first.click()
        vps=next(v for v in stack.client.get('/api/vps').json()['vps'] if v['name']=='Testing '+unique)
        # Configure the sample worker in one role editor.
        page.evaluate("navigate('access')")
        expect(page.locator('#role-create')).to_be_visible();page.click('#role-create')
        page.locator('#role-form [name=label]').fill('worker '+unique)
        page.click('#role-add-project-rule')
        p=page.locator('[data-project-rule]');p.locator('[name=project][value="'+stack.project['id']+'"]').check()
        p.locator('[name=action][value=write]').check();p.locator('[name=action][value=execute]').check()
        page.click('#role-add-vps-rule');v=page.locator('[data-vps-rule]')
        v.locator('[name=vps][value="'+vps['id']+'"]').check();v.locator('[name=action][value=execute]').check()
        page.locator('button[form=role-form]').click();expect(page.locator('#role-form')).to_have_count(0)
        expect(page.locator('#roles-page')).to_contain_text('worker '+unique)
        page.locator('[data-product-area=access][data-product-tab=connections]').click()
        page.click('#connection-create');f=page.locator('#connection-form')
        f.locator('[name=name]').fill('Client '+unique)
        saved=next(r for r in stack.client.get('/api/access-roles').json()['roles'] if r['label']=='worker '+unique)
        f.locator('[name=role_id]').select_option(saved['id'])
        expect(f.locator('#connection-policy')).to_contain_text('Testing '+unique)
        page.locator('button[form=connection-form]').click();expect(page.locator('#connection-copy')).to_be_visible()
        token=page.locator('.modal .secret').inner_text()
        page.locator('.modal [data-action=close-modal]').first.click()
        record=page.locator('[data-connection-id]').filter(has_text='Client '+unique)
        expect(record).to_contain_text('固定同意上限')
        expect(record).to_contain_text('Testing '+unique);expect(record).to_contain_text('execute')
        assert token not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
        assert 'FIXTURE_SSH_PASSWORD' not in page.content()
        # Actual requests carry simulated host metadata; execute a fixture command.
        response=stack.rpc('tools/call',{'name':'exec','arguments':{'project':'Imago','command':'printf product-browser-fixture',
            'yield_seconds':0,'idempotency_key':uuid.uuid4().hex},'_meta':{'openai/session':'browser-'+unique}},token_value=token)
        stack.must(response);result=response.json()['result'];assert not result.get('isError'),result
        operation=result['structuredContent']['operation_id'];stack.poll(operation)
        page.evaluate("navigate('conversations')");expect(page.locator('#conversations-page')).to_be_visible()
        page.locator('[data-conversation-detail]').filter(visible=True).first.click()
        expect(page.locator('.modal-body')).to_contain_text('Imago')
        expect(page.locator('.modal-body')).to_contain_text(operation)
        page.click('#conversation-edit');f=page.locator('#conversation-form')
        f.locator('[name=original_url]').fill('https://chatgpt.com/c/actual-supplied-'+unique)
        page.locator('button[form=conversation-form]').click();expect(f).to_have_count(0)
        link=page.get_by_role('link',name='返回原对话').first
        expect(link).to_have_attribute('href','https://chatgpt.com/c/actual-supplied-'+unique)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'product-{width}.png'),full_page=True)
        assert not errors,errors
    finally:page.close()


def test_old_workflow_hash_opens_archive_and_tools_panel_has_no_progress_entry(stack,chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    try:
        page.goto(stack.url+'/#workflows');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#conversations-page')).to_be_visible()
        expect(page.locator('#page')).to_contain_text('只供历史读取')
        assert page.evaluate('S.conversationTab')=='archive'
        assert not page.locator('[data-wf-action=create]').count()
        page.evaluate("navigate('integrations')");expect(page.locator('[data-i-tab=overview]')).to_be_visible()
        assert not page.locator('[data-i-tab=handoff],[data-i-action=new-workflow]').count()
        page.reload();expect(page.locator('[data-i-tab=overview]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    finally:page.close()


def test_resource_detail_intent_survives_passive_refresh(stack, chat_browser_pool):
    page = chat_browser_pool('chromium').new_page()
    held = []
    path = '/api/resources/project/' + stack.project['id']
    try:
        page.goto(stack.url + '/#resources/projects')
        page.fill('#username', 'admin'); page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('#resources-page')).to_be_visible()
        page.route('**' + path, lambda route: held.append(route))
        page.locator('[data-resource-detail="' + stack.project['id'] + '"]').click()
        page.wait_for_function('() => S.productIntent > 0')
        page.evaluate('renderPage(false)')
        assert held
        held.pop().fulfill(json=stack.must(stack.client.get(path)))
        expect(page.locator('.modal-body')).to_contain_text('Imago')
        expect(page.locator('.modal-body')).to_contain_text('操作与审计')
    finally:
        for route in held: route.abort()
        page.unroute_all(behavior='wait'); page.close()


def test_oauth_selects_role_and_creates_stable_identity_inline(stack,chat_browser_pool):
    import base64
    import hashlib
    from urllib.parse import parse_qs,urlsplit
    from shared.role_contracts import ROLE_SCOPE
    label='OAuth-'+uuid.uuid4().hex[:8]
    r=role(stack.client,label=label,project_rules=[{'actions':['read'],'projects':[stack.project['id']]}])
    page=chat_browser_pool('chromium').new_page()
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(stack.url+'/#access/connections');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#access-page')).to_be_visible()
        registered=stack.must(stack.client.post('/oauth/register',json={'redirect_uris':['http://localhost:12345/callback']}))
        verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        request=stack.client.get('/oauth/authorize',params={'response_type':'code','client_id':registered['client_id'],
            'redirect_uri':'http://localhost:12345/callback','scope':ROLE_SCOPE,'code_challenge_method':'S256',
            'code_challenge':challenge,'resource':stack.url+'/mcp'},follow_redirects=False)
        identifier=parse_qs(urlsplit(request.headers['location']).query)['authorize'][0]
        page.route('http://localhost:12345/callback*',lambda route:route.fulfill(body='Isolated OAuth callback'))
        before=len(stack.client.get('/api/access-profiles').json()['profiles'])
        page.evaluate('(id)=>consentModal(id)',identifier)
        page.select_option('#access-profile-selector','new:'+r['id'])
        checkbox=page.locator('[name=confirm_dynamic_role]');expect(checkbox).not_to_be_checked()
        page.click('#allow-consent');expect(checkbox).to_be_visible()
        assert len(stack.client.get('/api/access-profiles').json()['profiles'])==before
        checkbox.check()
        with page.expect_response(lambda response:response.url.endswith('/decide')) as received:
            page.click('#allow-consent')
        assert received.value.status==200
        page.wait_for_url('http://localhost:12345/callback*')
        code=parse_qs(urlsplit(page.url).query)['code'][0]
        exchange=stack.must(stack.client.post('/oauth/token',data={'grant_type':'authorization_code','code':code,
            'client_id':registered['client_id'],'redirect_uri':'http://localhost:12345/callback','code_verifier':verifier}))
        actual=stack.mcp('get_access_context',token_value=exchange['access_token'])['structuredContent']
        assert actual['role']['id']==r['id'] and actual['managed']
        assert actual['project_permissions'][0]['actions']==['read']
        assert len(stack.client.get('/api/access-profiles').json()['profiles'])==before+1
        assert not errors,errors
    finally:page.close()
