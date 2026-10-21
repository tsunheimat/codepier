"""Current Profile management and two-stage consent through real browser/HTTP."""
import base64
import hashlib
import json
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
    expect(form.locator('[name="role_id"],[name="scope"]')).to_have_count(0)
    expect(form.locator('[name="all_projects"]')).not_to_be_checked()
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


def new_profile_form(page, label):
    page.locator('[data-ws-action="new-profiles"]').click()
    form = page.locator('#workspace-form')
    expect(page.get_by_role('button', name='保存', exact=True)).to_be_enabled()
    form.locator('[name="label"]').fill(label)
    return form


def save_profile_form(page, stack, identifier=''):
    path = profile_url(stack, identifier)
    with page.expect_response(lambda r: r.url.endswith(path) and r.request.method == ('PUT' if identifier else 'POST')) as saved:
        page.get_by_role('button', name='保存', exact=True).click()
    assert saved.value.status in (200, 201), saved.value.text()
    expect(page.locator('#workspace-form')).to_have_count(0, timeout=15000)
    return saved.value.json()


def test_project_rwe_shortcuts_do_not_grant_computer_or_unrelated_project(profiles_page, stack):
    page, errors = profiles_page
    form = new_profile_form(page, 'Quick actions '+uuid.uuid4().hex)
    first = form.locator('fieldset').filter(has=page.locator('[data-policy-kind="projects"][data-resource="'+stack.project['id']+'"]')).last
    first.locator('[data-policy-rwe]').click()
    for action in ('read', 'write', 'execute'):
        expect(first.locator('[value="'+action+'"]')).to_be_checked()
    expect(first.locator('[value="computer"]')).not_to_be_checked()
    expect(form.locator('[data-policy-kind="projects"][data-resource="'+stack.projects[1]['id']+'"]:checked')).to_have_count(0)
    # A preset adds exactly the requested three actions, preserving a deliberate
    # existing computer selection rather than unexpectedly revoking it.
    first.locator('[value="computer"]').check()
    first.locator('[data-policy-rwe]').click()
    expect(first.locator('[value="computer"]')).to_be_checked()
    first.locator('[value="computer"]').uncheck()
    current = save_profile_form(page, stack)
    assert current['policy']['project_rules'] == [{
        'projects': [stack.project['id']], 'actions': ['read', 'write', 'execute'],
        'all_projects': False, 'excluded_projects': [],
    }]
    page.locator('[data-ws-action="profile"][data-id="'+current['id']+'"]').click()
    form = page.locator('#workspace-form')
    form.locator('[data-policy-rwe-listed]').click()
    current = save_profile_form(page, stack, current['id'])
    ids = {p['id'] for p in stack.must(stack.client.get('/api/spaces/'+stack.space_id+'/projects'))['projects']}
    assert {r['projects'][0] for r in current['policy']['project_rules']} == ids
    assert all(r['actions'] == ['read', 'write', 'execute'] and not r['all_projects'] for r in current['policy']['project_rules'])
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert errors == []


def test_all_projects_visual_roundtrip_exclusions_and_future_connection_consent(profiles_page, stack):
    page, errors = profiles_page
    form = new_profile_form(page, 'All projects '+uuid.uuid4().hex)
    form.locator('[name="all_projects"]').check()
    expect(form.locator('[data-policy-selected-projects]')).to_be_hidden()
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    excluded = stack.projects[1]['id']
    form.locator('[name="excluded_projects"][value="'+excluded+'"]').check()
    current = save_profile_form(page, stack)
    assert current['policy']['project_rules'] == [{
        'all_projects': True, 'projects': [], 'actions': ['read', 'write', 'execute'],
        'excluded_projects': [excluded],
    }]
    page.locator('[data-ws-action="profile"][data-id="'+current['id']+'"]').click()
    form = page.locator('#workspace-form')
    expect(form.locator('[name="raw_policy"]')).not_to_be_checked()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    expect(form.locator('[name="excluded_projects"][value="'+excluded+'"]')).to_be_checked()
    form.locator('[name="label"]').fill(current['label']+' renamed')
    updated = save_profile_form(page, stack, current['id'])
    assert updated['policy'] == current['policy']
    fixed = stack.create_connection(updated)
    live = stack.create_connection(updated, live=True)
    alias = 'Future'+uuid.uuid4().hex[:12]
    (stack.root/alias).mkdir()
    future = stack.create_project({'alias': alias, 'source_id': stack.folder['id'], 'subpath': alias})
    def visible(token):
        value = stack.mcp('get_access_context', token_value=token)['structuredContent']
        return {p['id'] for p in value['projects']}
    assert future['id'] not in visible(fixed['token'])
    assert future['id'] in visible(live['token'])
    assert excluded not in visible(fixed['token']) | visible(live['token'])
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert errors == []


def test_all_projects_requires_explicit_actions_before_saving(profiles_page):
    page, errors = profiles_page
    form = new_profile_form(page, 'No implicit permission '+uuid.uuid4().hex)
    writes = []
    # Listen only after the browser has completed loading the editor.
    page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' and r.url.endswith('/profiles') else None)
    form.locator('[name="all_projects"]').check()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(form.locator('[data-ws-status]')).to_contain_text('至少 read')
    assert writes == []
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    assert errors == []


def test_complex_profile_stays_in_json_and_preserves_unrelated_rules(profiles_page, stack):
    page, errors = profiles_page
    original = stack.create_profile({
        'project_rules': [
            {'all_projects': True, 'actions': ['read'], 'excluded_projects': [stack.projects[1]['id']]},
            {'projects': [stack.project['id']], 'actions': ['read', 'write', 'execute']},
        ],
        'source_rules': [{'sources': [stack.folder['id']], 'actions': ['read', 'projects.create']}],
        'vps_rules': [{'all_vps': True, 'actions': ['read']}],
    }, label='Mixed policy '+uuid.uuid4().hex)
    page.reload()
    page.locator('[data-ws-action="profile"][data-id="'+original['id']+'"]').click()
    form = page.locator('#workspace-form')
    expect(form.locator('[name="raw_policy"]')).to_be_checked()
    expect(form.locator('[data-policy-pairs]')).to_be_hidden()
    form.locator('[name="raw_policy"]').click()
    expect(form.locator('[name="raw_policy"]')).to_be_checked()
    expect(form.locator('[data-policy-status]')).to_contain_text('不会自动扩大或丢失权限')
    form.locator('[name="label"]').fill(original['label']+' renamed')
    updated = save_profile_form(page, stack, original['id'])
    assert updated['policy'] == original['policy']
    assert errors == []


def test_simple_json_visual_switch_keeps_draft_and_source_rules(profiles_page, stack):
    page, errors = profiles_page
    original = stack.create_profile({
        'project_rules': [{'projects': [stack.project['id']], 'actions': ['read']}],
        'source_rules': [{'sources': [stack.folder['id']], 'actions': ['read', 'projects.create']}],
    }, label='Mode switching '+uuid.uuid4().hex)
    page.reload()
    page.locator('[data-ws-action="profile"][data-id="'+original['id']+'"]').click()
    form = page.locator('#workspace-form')
    form.locator('[name="all_projects"]').check()
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    form.locator('[name="excluded_projects"][value="'+stack.projects[2]['id']+'"]').check()
    form.locator('[name="raw_policy"]').check()
    draft = json.loads(form.locator('[name="policy_extra"]').input_value())
    assert draft['project_rules'][0]['all_projects']
    assert draft['project_rules'][0]['excluded_projects'] == [stack.projects[2]['id']]
    assert draft['source_rules'] == original['policy']['source_rules']
    form.locator('[name="policy_extra"]').fill('{invalid')
    form.locator('[name="raw_policy"]').click()
    expect(form.locator('[name="raw_policy"]')).to_be_checked()
    expect(form.locator('[name="policy_extra"]')).to_have_value('{invalid')
    form.locator('[name="policy_extra"]').fill(json.dumps(draft))
    form.locator('[name="raw_policy"]').uncheck()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    updated = save_profile_form(page, stack, original['id'])
    assert updated['policy']['source_rules'] == original['policy']['source_rules']
    assert updated['policy']['project_rules'][0]['all_projects']
    assert updated['policy']['project_rules'][0]['excluded_projects'] == [stack.projects[2]['id']]
    assert errors == []


def test_connections_page_shows_and_copies_canonical_url_with_empty_list(profiles_page, stack):
    page, errors = profiles_page
    empty_space = stack.must(stack.client.post('/api/spaces', json={
        'label': 'No connections '+uuid.uuid4().hex, 'idempotency_key': uuid.uuid4().hex,
    }))
    page.goto(stack.url+'/#spaces/'+empty_space['id']+'/connections')
    expected = stack.must(stack.client.get('/api/settings'))['mcp_url']
    field = page.locator('#workspace-mcp-url')
    expect(field).to_have_value(expected)
    assert field.get_attribute('readonly') is not None
    assert '?' not in field.input_value()
    assert stack.must(stack.client.get('/api/spaces/'+empty_space['id']+'/connections'))['connections'] == []
    # Intercept clipboard writes only; URL and page data come from the real Hub.
    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
        configurable: true, value: { writeText: async text => { window.copiedMcpUrl = text; } }
    })""")
    page.locator('[data-ws-action="copy-mcp-url"]').click()
    page.wait_for_function('window.copiedMcpUrl !== undefined')
    assert page.evaluate('window.copiedMcpUrl') == expected
    expect(page.locator('#workspace-page')).to_contain_text('OAuth')
    expect(page.locator('#workspace-page')).to_contain_text('DCR')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert errors == []


# These controls edit the existing policy contract; they never change consent
# mode, add management rights or convert complex policies into broader grants.
def open_profile_editor(page, stack, profile=None):
    selector='[data-ws-action="profile"][data-id="'+profile['id']+'"]' if profile else '[data-ws-action="new-profiles"]'
    if profile:
        # This fixture already opened the Profiles page. Reload once to fetch
        # API-created entries; a same-URL goto followed immediately by reload
        # interrupts WebKit's real account bootstrap request.
        page.reload()
    page.locator(selector).click()
    form=page.locator('#workspace-form')
    expect(page.get_by_role('button',name='保存',exact=True)).to_be_enabled(timeout=15000)
    return form


def save_profile_editor(page, stack):
    with page.expect_response(lambda r:r.request.method in {'POST','PUT'} and '/profiles' in r.url) as received:
        page.get_by_role('button',name='保存',exact=True).click()
    assert received.value.status in {200,201},received.value.text()
    expect(page.locator('#workspace-form')).to_have_count(0,timeout=15000)
    return received.value.json()


def test_project_quick_actions_do_not_grant_computer_or_other_resource_actions(profiles_page,stack):
    page,errors=profiles_page
    form=open_profile_editor(page,stack)
    form.locator('[name="label"]').fill('Quick actions '+uuid.uuid4().hex)
    first=form.locator('[data-policy-kind="projects"][data-resource="'+stack.project['id']+'"]').first.locator('..').locator('..')
    first.locator('[data-policy-rwe]').click()
    assert first.locator('[data-policy-kind]:checked').evaluate_all('(nodes) => nodes.map(n=>n.value)')==['read','write','execute']
    expect(form.locator('[data-policy-kind="vps"]:checked')).to_have_count(0)
    expect(form.locator('[data-policy-kind="projects"][value="computer"]:checked')).to_have_count(0)
    other=form.locator('[data-policy-kind="projects"][data-resource="'+stack.projects[1]['id']+'"]').first.locator('..').locator('..')
    expect(other.locator('input:checked')).to_have_count(0)
    first.locator('[value="computer"]').check()
    first.locator('[data-policy-rwe]').click()
    expect(first.locator('[value="computer"]')).to_be_checked()
    first.locator('[value="computer"]').uncheck()
    first.locator('[value="read"]').uncheck()
    expect(first.locator('input:checked')).to_have_count(0)
    form.locator('[data-policy-rwe-listed]').click()
    result=save_profile_editor(page,stack)
    ids={p['id'] for p in stack.must(stack.client.get('/api/spaces/'+stack.space_id+'/projects'))['projects']}
    rules=result['policy']['project_rules']
    assert {p for r in rules for p in r['projects']}==ids
    assert all(r['actions']==['read','write','execute'] and not r['all_projects'] for r in rules)
    assert result['policy']['vps_rules']==result['policy']['manage']==[]
    assert errors==[]


def test_all_projects_visual_roundtrip_preserves_exclusions_and_source_rules(profiles_page,stack):
    page,errors=profiles_page
    excluded=stack.projects[1]['id']
    profile=stack.create_profile({'project_rules':[{'all_projects':True,'actions':['read'],'excluded_projects':[excluded]}],
        'source_rules':[{'sources':[stack.folder['id']],'actions':['read','projects.create']}]})
    form=open_profile_editor(page,stack,profile)
    expect(form.locator('[name="raw_policy"]')).not_to_be_checked()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    expect(form.locator('[name="excluded_projects"][value="'+excluded+'"]')).to_be_checked()
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    expect(form.locator('[data-policy-kind="all-projects"][value="computer"]')).not_to_be_checked()
    result=save_profile_editor(page,stack)
    assert result['policy']['project_rules']==[{'all_projects':True,'projects':[],
        'excluded_projects':[excluded],'actions':['read','write','execute']}]
    assert result['policy']['source_rules']==profile['policy']['source_rules']
    form=open_profile_editor(page,stack,result)
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    expect(form.locator('[name="excluded_projects"][value="'+excluded+'"]')).to_be_checked()
    form.locator('[name="raw_policy"]').check()
    assert json.loads(form.locator('[name="policy_extra"]').input_value())==result['policy']
    form.locator('[name="raw_policy"]').uncheck()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    form.locator('[name="label"]').fill(result['label']+' renamed')
    renamed=save_profile_editor(page,stack)
    assert renamed['policy']==result['policy']
    assert errors==[]


def test_visual_all_projects_respects_fixed_live_and_member_permission_limits(profiles_page,stack):
    page,errors=profiles_page
    form=open_profile_editor(page,stack)
    form.locator('[name="label"]').fill('Future projects '+uuid.uuid4().hex)
    form.locator('[name="all_projects"]').check()
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    excluded=stack.projects[1]['id']
    form.locator('[name="excluded_projects"][value="'+excluded+'"]' ).check()
    profile=save_profile_editor(page,stack)
    fixed=stack.create_connection(profile)
    live=stack.create_connection(profile,live=True)
    future=stack.create_project({'alias':'Future-'+uuid.uuid4().hex[:10],
        'source_id':stack.folder['id'],'subpath':stack.project['alias']})
    def context(connection):
        return stack.rpc('tools/call',{'name':'get_access_context','arguments':{}},
            token_value=connection['token']).json()['result']['structuredContent']
    fixed_context=context(fixed);live_context=context(live)
    assert future['id'] not in {p['id'] for p in fixed_context['projects']}
    assert future['id'] in {p['id'] for p in live_context['projects']}
    assert excluded not in {p['id'] for p in live_context['projects']}
    members_path='/api/spaces/'+stack.space_id+'/members'
    before=next(m for m in stack.must(stack.client.get(members_path))['members'] if m['username']=='admin' and m['source']=='manual')
    member_path=members_path+'/'+before['user_id']
    restricted=stack.must(stack.client.put(member_path,json={'policy':{**before['policy'],
        'project_rules':[{'all_projects':True,'actions':['read']}]},'active':bool(before['active']),
        'expected_version':before['version'],'idempotency_key':uuid.uuid4().hex}))
    try:
        narrowed=context(live)
        assert narrowed['scopes']==['read']
        assert future['id'] in {p['id'] for p in narrowed['projects']}
        assert narrowed['connection']['id']==live_context['connection']['id']
    finally:
        stack.must(stack.client.put(member_path,json={'policy':before['policy'],'active':bool(before['active']),
            'expected_version':restricted['version'],'idempotency_key':uuid.uuid4().hex}))
    assert errors==[]


def test_mixed_policy_remains_json_and_cannot_be_silently_flattened(profiles_page,stack):
    page,errors=profiles_page
    profile=stack.create_profile({'project_rules':[
        {'all_projects':True,'actions':['read'],'excluded_projects':[stack.project['id']]},
        {'projects':[stack.project['id']],'actions':['read','execute']}],
        'vps_rules':[{'all_vps':True,'actions':['read']}]})
    form=open_profile_editor(page,stack,profile)
    expect(form.locator('[name="raw_policy"]')).to_be_checked()
    expect(form.locator('[data-policy-pairs]')).to_be_hidden()
    form.locator('[name="raw_policy"]').click()
    expect(form.locator('[name="raw_policy"]')).to_be_checked()
    expect(form.locator('[data-policy-status]')).to_contain_text('不会自动扩大或丢失权限')
    assert json.loads(form.locator('[name="policy_extra"]').input_value())==profile['policy']
    form.locator('[name="label"]').fill(profile['label']+' renamed')
    result=save_profile_editor(page,stack)
    assert result['policy']==profile['policy']
    assert errors==[]


def test_json_and_visual_transitions_preserve_manual_drafts(profiles_page,stack):
    page,errors=profiles_page
    form=open_profile_editor(page,stack)
    form.locator('[name="label"]').fill('JSON roundtrip '+uuid.uuid4().hex)
    form.locator('[aria-label="高级策略配置"] summary').click()
    policy={'project_rules':[{'all_projects':True,'actions':['read'],
        'excluded_projects':[stack.projects[2]['id']]}],
        'source_rules':[{'sources':[stack.folder['id']],'actions':['read']}]}
    manual=json.dumps(policy,indent=2)
    form.locator('[name="policy_extra"]').fill(manual)
    form.locator('[name="raw_policy"]').check()
    assert form.locator('[name="policy_extra"]').input_value()==manual
    form.locator('[name="raw_policy"]').uncheck()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    form.locator('[name="raw_policy"]').check()
    synced=json.loads(form.locator('[name="policy_extra"]').input_value())
    assert synced['project_rules'][0]['actions']==['read','write','execute']
    assert synced['source_rules']==policy['source_rules']
    for unsupported in [
        {'project_rules':[{'all_projects':True,'actions':['read','root']}]},
        {'project_rules':[{'all_projects':True,'actions':['read'],'unknown_selector':True}]},
        {'project_rules':'invalid'},
    ]:
        encoded=json.dumps(unsupported)
        form.locator('[name="policy_extra"]').fill(encoded)
        form.locator('[name="raw_policy"]').click()
        expect(form.locator('[name="raw_policy"]')).to_be_checked()
        assert form.locator('[name="policy_extra"]').input_value()==encoded
    form.locator('[name="policy_extra"]').fill(json.dumps(synced))
    form.locator('[name="raw_policy"]').uncheck()
    result=save_profile_editor(page,stack)
    assert result['policy']['project_rules'][0]['all_projects']
    assert result['policy']['project_rules'][0]['excluded_projects']==[stack.projects[2]['id']]
    assert result['policy']['source_rules']==policy['source_rules']
    assert errors==[]


def test_all_projects_can_be_set_in_empty_workspace_but_never_implies_actions(profiles_page,stack):
    page,errors=profiles_page
    space=stack.must(stack.client.post('/api/spaces',json={'label':'Empty UI '+uuid.uuid4().hex,'idempotency_key':uuid.uuid4().hex}))
    page.goto(stack.url+'/#spaces/'+space['id']+'/profiles')
    page.locator('[data-ws-action="new-profiles"]').click()
    form=page.locator('#workspace-form')
    expect(page.get_by_role('button',name='保存',exact=True)).to_be_enabled()
    form.locator('[name="label"]').fill('Empty scope')
    form.locator('[name="all_projects"]').check()
    expect(form.locator('[data-policy-kind="all-projects"]:checked')).to_have_count(0)
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form.locator('[data-ws-status]')).to_contain_text('未选择不表示允许全部')
    assert stack.must(stack.client.get('/api/spaces/'+space['id']+'/profiles'))['profiles']==[]
    form.locator('[data-policy-all-projects] [data-policy-rwe]').click()
    result=save_profile_editor(page,stack)
    assert result['policy']['project_rules']==[{'projects':[],'all_projects':True,
        'excluded_projects':[],'actions':['read','write','execute']}]
    assert errors==[]


def test_connections_show_and_copy_server_public_url_not_browser_origin(profiles_page,stack):
    page,errors=profiles_page
    previous=stack.must(stack.client.get('/api/settings'))['public_url']
    public='https://mcp-connection-fixture.example'
    stack.must(stack.client.put('/api/settings',json={'public_url':public}))
    try:
        page.goto(stack.url+'/#spaces/'+stack.space_id+'/connections')
        field=page.get_by_label('MCP Server URL',exact=True)
        expect(field).to_have_value(public+'/mcp')
        expect(field).to_have_attribute('readonly','')
        assert field.input_value()!=stack.url+'/mcp' and '?' not in field.input_value()
        page.evaluate('''() => {
            window.copiedMcpUrl = null;
            Object.defineProperty(navigator, 'clipboard', {configurable: true,
                value: {writeText: async text => { window.copiedMcpUrl = text; }}});
        }''')
        page.get_by_role('button',name='复制 MCP 地址',exact=True).click()
        page.wait_for_function('window.copiedMcpUrl === '+json.dumps(public+'/mcp'))
        expect(page.locator('#workspace-page')).to_contain_text('不要添加 authorization=role')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    finally:
        stack.must(stack.client.put('/api/settings',json={'public_url':previous}))
    assert errors==[]
