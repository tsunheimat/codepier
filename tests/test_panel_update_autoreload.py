"""Update recovery and unsaved-input protection in real Chromium and WebKit."""
import re

import pytest
from playwright.sync_api import expect

from shared.util import VERSION
from tests.test_panel_update_ui import open_settings, ready, update_browser


def complete(state, version='1.11.0'):
    state.update(enabled=True, busy=False, request_found=True, running_version=version,
                 current_version=version, update_available=False,
                 operation={'id': 'f' * 32, 'kind': 'apply', 'state': 'succeeded',
                            'target_version': version, 'message': '更新成功', 'events': []})


@pytest.mark.parametrize('dirty_field', ['address', 'password', 'draft'])
def test_auto_refresh_defers_unsaved_values_and_resumes_after_resolution(update_browser, stack, dirty_field):
    browser, _ = update_browser
    state = ready()
    page, calls, errors = open_settings(browser, stack, state)
    try:
        if dirty_field == 'address':
            page.fill('#settings-form [name="public_url"]', stack.url + '/unsaved')
        elif dirty_field == 'password':
            page.fill('#password-form [name="current_password"]', 'unsaved-password')
        else:
            page.evaluate('S.work.dirty=true')
        complete(state)
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-note')).to_contain_text('暂缓自动刷新')
        assert '_codepier_updated=' not in page.url
        if dirty_field == 'address':
            expect(page.locator('#settings-form [name="public_url"]')).to_have_value(stack.url + '/unsaved')
            page.fill('#settings-form [name="public_url"]', stack.url)
        elif dirty_field == 'password':
            expect(page.locator('#password-form [name="current_password"]')).to_have_value('unsaved-password')
            page.fill('#password-form [name="current_password"]', '')
        else:
            page.evaluate('S.work.dirty=false')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'), timeout=12000)
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        assert not calls and not errors, errors
    finally:
        page.close()


def test_no_early_reload_and_monitor_survives_navigation_and_service_recovery(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    complete(state)
    state['running_version'] = VERSION
    page, calls, errors = open_settings(browser, stack, state)
    try:
        expect(page.locator('#panel-update-note')).to_contain_text('等待目标版本')
        assert '_codepier_updated=' not in page.url
        page.evaluate("navigate('resources/devices')")
        expect(page.locator('#page h1')).to_have_text('资源库')
        expect(page.get_by_role('navigation',name='资源类别').locator('[data-nav="resources/devices"]')).to_have_attribute('aria-pressed','true')
        # A successful HTTP response with unavailable updater is not an update failure.
        state.update(enabled=False, code='UPDATER_UNAVAILABLE')
        page.wait_for_timeout(2400)
        assert '_codepier_updated=' not in page.url
        complete(state)
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#resources/devices$'), timeout=12000)
        expect(page.locator('#page h1')).to_have_text('资源库')
        expect(page.get_by_role('navigation',name='资源类别').locator('[data-nav="resources/devices"]')).to_have_attribute('aria-pressed','true')
        assert not calls and not errors, errors
    finally:
        page.close()


def test_completed_update_does_not_reload_loop_when_storage_is_unavailable(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    script = """(() => {
      const get=Storage.prototype.getItem, set=Storage.prototype.setItem;
      Storage.prototype.getItem=function(key){if(String(key).startsWith('codepier-panel-update:'))throw new Error('storage unavailable');return get.call(this,key);};
      Storage.prototype.setItem=function(key,value){if(String(key).startsWith('codepier-panel-update:'))throw new Error('storage unavailable');return set.call(this,key,value);};
    })();"""
    page, calls, errors = open_settings(browser, stack, state, init_script=script)
    documents = []
    page.on('request', lambda request: documents.append(request.url) if request.resource_type == 'document' else None)
    try:
        complete(state)
        page.click('#panel-update-refresh')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'), timeout=12000)
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        page.click('#panel-update-refresh')
        page.wait_for_timeout(2400)
        assert len(documents) == 1, documents
        assert not calls and not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize('terminal_state', ['failed', 'rolled_back', 'recovery_required'])
def test_failed_or_rolled_back_update_never_auto_reloads(update_browser, stack, terminal_state):
    browser, _ = update_browser
    state = ready()
    page, calls, errors = open_settings(browser, stack, state)
    try:
        complete(state)
        state['operation'].update(state=terminal_state, message=terminal_state)
        state['recovery_required'] = terminal_state == 'recovery_required'
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-state')).to_contain_text(terminal_state)
        expect(page.locator('#panel-update-reload')).to_be_hidden()
        assert '_codepier_updated=' not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()


def test_already_current_document_does_not_reload_for_historic_success(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    complete(state, VERSION)
    page, calls, errors = open_settings(browser, stack, state)
    try:
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        page.click('#panel-update-refresh')
        assert '_codepier_updated=' not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()


def test_auto_refresh_keeps_an_unsaved_workspace_profile_until_explicit_discard(update_browser,stack):
    browser,_=update_browser;state=ready()
    # Start from an actual observed update in flight. An idle settings visit
    # does not grant authority to monitor/reload every future unrelated view.
    state.update(busy=True,request_found=True,operation={'id':'f'*32,'kind':'apply',
        'state':'running','target_version':'1.11.0','message':'正在构建','events':[]})
    page,calls,errors=open_settings(browser,stack,state)
    expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
    path='/api/spaces/'+stack.space_id+'/profiles'
    before=stack.must(stack.client.get(path))['profiles']
    try:
        page.evaluate('(route)=>navigate(route)','spaces/'+stack.space_id+'/profiles')
        page.locator('[data-ws-action="new-profiles"]').click()
        form=page.locator('#workspace-form')
        form.locator('[name="label"]').fill('UNSAVED_PROFILE_DRAFT')
        complete(state)
        # The persistent update monitor must actually observe completion, not
        # merely be asleep during this assertion. No production updater runs.
        for _ in range(2):
            with page.expect_response(lambda response:'/api/panel-update/' in response.url and
                                      response.request.method=='GET') as response:
                pass
            assert response.value.json()['operation']['state']=='succeeded'
        expect(form.locator('[name="label"]')).to_have_value('UNSAVED_PROFILE_DRAFT')
        assert '_codepier_updated=' not in page.url
        page.once('dialog',lambda dialog:dialog.accept())
        page.keyboard.press('Escape')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#spaces/'+stack.space_id+'/profiles$'),timeout=12000)
        expect(page.locator('#workspace-page h2')).to_have_text('Profiles')
        assert stack.must(stack.client.get(path))['profiles']==before
        assert not calls and not errors,errors
    finally:page.close()
