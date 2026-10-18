"""Current Profile management and two-stage consent through real browser/HTTP."""
import base64
import hashlib
import re
import uuid
from urllib.parse import parse_qs,urlsplit

import pytest
from playwright.sync_api import expect


@pytest.fixture(params=['chromium','webkit'])
def profiles_browser(request,chat_browser_pool):
    return chat_browser_pool(request.param)


@pytest.fixture(params=[{'width':1280,'height':900},{'width':390,'height':844}])
def profiles_page(profiles_browser,request,stack):
    page=profiles_browser.new_page(viewport=request.param);errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(stack.url+'/#spaces/'+stack.space_id+'/profiles')
    page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#workspace-page h2')).to_have_text('Profiles',timeout=15000)
    try:yield page,errors
    finally:page.close()


def profile_url(stack,identifier=''):
    return '/api/spaces/'+stack.space_id+'/profiles'+('/'+identifier if identifier else '')


def test_create_rename_and_concurrent_edit_keep_identity(profiles_page,stack):
    page,errors=profiles_page;label='Review '+uuid.uuid4().hex[:10]
    page.locator('[data-ws-action="new-profiles"]').click();form=page.locator('#workspace-form')
    expect(form.locator('[name="role_id"],[name="all_projects"],[name="scope"]')).to_have_count(0)
    expect(form.locator('[data-policy-kind]:checked')).to_have_count(0)
    form.locator('[name="label"]').fill(label)
    form.locator('[data-policy-kind="projects"][data-resource="'+stack.project['id']+'"][value="read"]').check()
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form).to_have_count(0,timeout=15000)
    current=next(p for p in stack.must(stack.client.get(profile_url(stack)))['profiles'] if p['label']==label)
    identifier=current['id']
    assert current['policy']['project_rules']==[{'projects':[stack.project['id']],'actions':['read'],
        'all_projects':False,'excluded_projects':[]}]
    page.locator('[data-ws-action="profile"][data-id="'+identifier+'"]').click()
    form.locator('[name="label"]').fill(label+' renamed')
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form).to_have_count(0,timeout=15000)
    expect(page.locator('#workspace-page h3').filter(has_text=label+' renamed')).to_be_visible()
    assert stack.must(stack.client.get(profile_url(stack,identifier)))['id']==identifier
    page.locator('[data-ws-action="profile"][data-id="'+identifier+'"]').click()
    form.locator('[name="label"]').fill('Local unsaved draft')
    current=stack.must(stack.client.get(profile_url(stack,identifier)))
    updated=stack.must(stack.client.put(profile_url(stack,identifier),json={
        'label':current['label'],'policy':current['policy'],'enabled':False,
        'expected_version':current['version'],'idempotency_key':uuid.uuid4().hex}))
    with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith(profile_url(stack,identifier))) as received:
        page.get_by_role('button',name='保存',exact=True).click()
    assert received.value.status==409
    expect(form.locator('[data-ws-status]')).to_have_text(received.value.json()['error']['message'])
    expect(form.locator('[name="label"]')).to_have_value('Local unsaved draft')
    assert stack.must(stack.client.get(profile_url(stack,identifier)))==updated
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert errors==[]


def test_oauth_profile_resource_pairs_bind_real_connection_without_global_scopes(profiles_page,stack):
    page,errors=profiles_page
    profile=stack.create_profile({'project_rules':[{'projects':[stack.project['id']],
        'actions':['read','write']}]},label='Consent '+uuid.uuid4().hex)
    registration=stack.must(stack.client.post('/oauth/register',json={'redirect_uris':['http://localhost:12345/callback']}))
    verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    pending=stack.client.get('/oauth/authorize',params={'response_type':'code','client_id':registration['client_id'],
        'redirect_uri':'http://localhost:12345/callback','scope':'codepier.workspace','code_challenge_method':'S256',
        'code_challenge':challenge,'resource':stack.url+'/mcp'},follow_redirects=False)
    assert pending.status_code==303,pending.text
    page.route('http://localhost:12345/callback*',lambda route:route.fulfill(body='Authorized fixture callback'))
    page.goto(stack.url+pending.headers['location'])
    expect(page.locator('[name="consent_profile"]')).to_be_disabled()
    expect(page.locator('[data-consent-allow]')).to_be_disabled()
    page.select_option('[name="consent_space"]',stack.space_id)
    page.select_option('[name="consent_profile"]',profile['id'])
    expect(page.locator('[name="scope"],[name="all_projects"],[name="project"]')).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    with page.expect_response(lambda r:'/api/oauth/requests/' in r.url and r.url.endswith('/decide')) as received:
        page.locator('[data-consent-allow]').click()
    assert received.value.status==200
    sent=received.value.request.post_data_json
    assert sent['space_id']==stack.space_id and sent['profile_id']==profile['id'] and sent['profile_version']==profile['version']
    assert not {'scopes','projects','role_id','authorization_mode'}&sent.keys()
    page.wait_for_url(re.compile(r'^http://localhost:12345/callback\?'))
    code=parse_qs(urlsplit(page.url).query)['code'][0]
    tokens=stack.must(stack.client.post('/oauth/token',data={'grant_type':'authorization_code','code':code,
        'client_id':registration['client_id'],'redirect_uri':'http://localhost:12345/callback',
        'resource':stack.url+'/mcp','code_verifier':verifier}))
    context=stack.rpc('tools/call',{'name':'get_access_context','arguments':{}},token_value=tokens['access_token']).json()['result']['structuredContent']
    assert context['profile']['id']==profile['id'] and context['connection']['id']!=profile['id']
    assert context['scopes']==['read','write'] and [p['id'] for p in context['projects']]==[stack.project['id']]
    assert tokens['scope']=='codepier.workspace'
    denied=stack.mcp('exec',{'project':stack.project['id'],'command':'must-not-run',
        'idempotency_key':uuid.uuid4().hex,'yield_seconds':0},token_value=tokens['access_token'])
    assert denied['isError']
    assert errors==[]
