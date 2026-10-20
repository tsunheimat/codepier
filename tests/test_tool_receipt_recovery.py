"""F19/F20/F21/F27: truthful recovery, cleanup and readable exports."""
import asyncio
import json
import shlex
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from tests.test_chat_sse import service, native_auth, request, sync  # noqa: F401
from tests.test_devtools_browser_actions import browser_page  # noqa: F401
from tests.test_devtools_flow import tools_page  # noqa: F401
from tests.test_integrations_stack import integrated_stack  # noqa: F401
from tests.test_project_cleanup_and_recovery import records_fixture

from tests.test_workspace_browser import workspace_server,workspace_browser,open_workspace  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt',['md','json'])
async def test_export_reconciles_deltas_without_duplicating_the_final_answer(service,fmt):
    from hub.native_cli import make_native_router
    obj,_project,sid=service
    frames=[{'type':'delta','receipt':'one','item_id':'answer','text':'Hello '},
            {'type':'delta','receipt':'one','item_id':'answer','text':'world'},
            {'type':'message','receipt':'one','item_id':'answer','text':'Hello world'},
            {'type':'done','receipt':'one','status':'completed'},
            {'type':'delta','receipt':'two','item_id':'answer','text':'Next '},
            {'type':'message','receipt':'two','item_id':'answer','text':'Next answer'},
            {'type':'done','receipt':'two','status':'completed'}]
    await sync(obj,sid,''.join(json.dumps(frame)+'\n' for frame in frames).encode())
    endpoint=next(route.endpoint for route in make_native_router(native_auth(obj),obj.runtime).routes if route.path.endswith('/export'))
    response=await endpoint(sid,request(),fmt)
    data=''.join([chunk async for chunk in response.body_iterator])
    if fmt=='md':
        assert data.count('Hello world')==1 and data.count('Next answer')==1
        assert data.count('Hello ')==1 and data.count('Next ')==1
        assert data.index('Hello world')<data.index('Next answer')
    else:
        decoded=json.loads(data)
        assert len(decoded)==len(frames)
        assert [event.get('text') for event in decoded]==[event.get('text') for event in frames]


def test_project_stop_skips_legacy_closed_receipts_but_rechecks_explicit_cleanup_failure():
    from agent.background_browser import BrowserBroker
    records,db=records_fixture()
    project={'id':'fixture','root':'/fixture','_coding_device':'device','_integration_owner':'owner'}
    broker=BrowserBroker(SimpleNamespace(config={}),records);calls=[]
    async def close(action,body,**kwargs):
        assert action=='close';calls.append(body['lease_id']);return {'tab_cleanup_confirmed':True}
    broker.rpc=close
    try:
        for index,extra in enumerate([{}, {'tab_cleanup_confirmed':True}, {'tab_cleanup_confirmed':False}],1):
            records.save('browser',f'{index:032x}',project,{'lease_id':f'{index:032x}','state':'closed',**extra})
        asyncio.run(broker.release_project(project))
        assert calls==[f'{3:032x}']
        assert asyncio.run(broker.release_project(project))==[]
    finally:db.close()


def test_failed_vps_receipt_is_observed_before_an_explicit_new_command(workspace_server,workspace_browser):
    server=workspace_server;context,errors=workspace_browser
    page=open_workspace(context,server,'spaces/alice/vps');calls=[]
    page.on('request',lambda request:calls.append(request.post_data_json)
        if request.method=='POST' and request.url.endswith('/api/tools/call') else None)
    marker=server.directory/'command-receipt-count'
    button=page.locator('[data-ws-action="vps-command"][data-id="'+'a'*32+'"]')
    button.click();form=page.locator('#workspace-form')
    form.locator('[name="command"]').fill('sleep 2; printf failed >> '+shlex.quote(str(marker))+'; exit 7')
    page.get_by_role('button',name='保存',exact=True).click()
    expect(form.locator('[data-ws-error-receipt]')).to_be_visible(timeout=30000)
    submitted=[call for call in calls if call['tool']=='exec']
    observed=[call for call in calls if call['tool']=='operations_wait']
    assert len(submitted)==1 and observed
    original=observed[0]['arguments']['operation_id']
    assert all(call['arguments']['operation_id']==original for call in observed)
    assert marker.read_text()=='failed'
    receipt=server.must(server.client.get('/api/operations/'+original))
    assert receipt['state']=='failed' and receipt['result']['data']['exit_code']==7
    assert form.locator('[data-ws-error-receipt]').get_attribute('data-ws-error-receipt')==original
    form.locator('[data-ws-error-receipt]').click()
    # The form status already contains the operation ID. Require the actual
    # authorized reader, not that pre-existing text, before dismissing it.
    expect(page.locator('#modal-title')).to_have_text('操作详情 · exec')
    expect(form).to_have_count(0)
    expect(page.locator('.modal-body')).to_contain_text(original)
    assert len([call for call in calls if call['tool']=='exec'])==1
    page.keyboard.press('Escape')
    expect(page.locator('.modal')).to_have_count(0)
    button.click()
    form.locator('[name="command"]').fill('printf success >> '+shlex.quote(str(marker))+'; printf fresh-result')
    page.get_by_role('button',name='保存',exact=True).click()
    expect(page.locator('#modal-title')).to_have_text('SSH 结果',timeout=30000)
    expect(page.locator('.modal-body')).to_contain_text('fresh-result')
    submitted=[call for call in calls if call['tool']=='exec']
    assert len(submitted)==2
    assert submitted[0]['arguments']['idempotency_key']!=submitted[1]['arguments']['idempotency_key']
    assert all(call['arguments']['target']=='vps:'+'a'*32 for call in submitted)
    assert all('project' not in call['arguments'] for call in submitted)
    assert marker.read_text()=='failedsuccess'
    assert server.must(server.client.get('/api/operations/'+original))['state']=='failed'
    assert errors==[]


def test_definite_browser_rejection_is_not_persisted_as_an_unknown_request(browser_page):
    page=browser_page
    page.evaluate('''() => {window.acceptBrowserOpen=toolHandlers.browser_open;
      toolHandlers.browser_open=async()=>{throw Object.assign(new Error('fixture device offline'),
        {code:'COMPUTER_OFFLINE',status:409,admitted:false});};}''')
    page.fill('[name=url]','https://example.test/first')
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-open] .integration-form-error')).to_contain_text('fixture device offline')
    assert not page.evaluate('[...S.integrations.submissions.values()].some(row=>!row.done)')
    assert page.locator('#i-receipts button').count()==0
    page.evaluate('''() => {toolHandlers.browser_open=acceptBrowserOpen;}''')
    page.evaluate('renderPage(false)')
    page.fill('[name=url]','https://example.test/second')
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-action]')).to_be_visible()
    calls=page.evaluate("toolCalls.filter(row=>row.tool==='browser_open').map(row=>row.arguments)")
    assert len(calls)==2 and calls[0]['idempotency_key']!=calls[1]['idempotency_key']
