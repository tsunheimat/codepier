"""Real local Hub/Agent receipts and read-only workflow retirement compatibility."""
import json
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from playwright.sync_api import expect

from hub.store import Store
from shared.contracts import OUTPUT_SCHEMAS
from tests.historical_workflows import seed_workflow
from tests.support import wait_for


def mcp(stack,name,args):
    from hub.core_tools import public_call
    public_name,public_args=public_call(name,args)
    result=stack.mcp(public_name,public_args)
    data=result['structuredContent']
    if public_name in {'process','task_query'} and 'operations' in data:data=data['operations'][0]
    assert not isinstance(data.get('error'),dict),result
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(data)
    return data


def archive(stack,title='Historical fixture'):
    store=Store(stack.hubdir)
    try:
        grant=store.one('SELECT * FROM grants WHERE id=?',(stack.grant,))
        return seed_workflow(store,project_id=stack.project['id'],user_id=grant['user_id'],grant_id=stack.grant,
                             title=title,key=uuid.uuid4().hex)
    finally:store.close()


def test_mcp_context_and_retired_workflow_write_are_independent(stack):
    context=mcp(stack,'project_context',{'project':'Imago','max_chars':1000})
    if context.get('pending'):
        op=stack.poll(context['operation_id']);context=op['result']['data']
    assert context['documents'][0]['path']=='README.md'
    retired=stack.mcp('workspace',{'project':'Imago','operation':'workflow_create',
        'options':{'title':'No new tracking','goal':'No stored progress'},'idempotency_key':uuid.uuid4().hex})
    assert retired['isError'] and retired['structuredContent']['error']['code']=='WORKFLOW_RETIRED'


def test_real_command_failure_retains_true_exit_and_compact_receipt(stack):
    submitted=mcp(stack,'tasks_run',{'project':'Imago','task':'failure','idempotency_key':uuid.uuid4().hex})
    op=stack.poll(submitted['operation_id'])
    assert op['state']=='failed' and op['result']['data']['exit_code']==3
    assert not op['result']['data']['command_ok']
    compact=mcp(stack,'operations_get',{'operation_id':op['id'],'include_result':False,'include_output':False})
    assert 'output' not in compact and 'result' not in compact


def test_archive_survives_actual_hub_restart_with_no_new_progress(stack):
    receipt=archive(stack,'Restart history')
    before=mcp(stack,'workflows_get',{'workflow_id':receipt['workflow_id']})
    stack.hub.terminate();stack.hub.wait(timeout=12);stack.start_hub()
    restored=mcp(stack,'workflows_get',{'workflow_id':receipt['workflow_id']})
    assert restored==before and restored['retired'] and not restored['can_update']
    assert restored['next_step'] is None
    error=stack.mcp('workspace',{'operation':'workflow_update','options':{'workflow_id':receipt['workflow_id'],
        'expected_version':receipt['version'],'action':'resume','summary':'Must not update'},'idempotency_key':uuid.uuid4().hex})
    assert error['isError'] and error['structuredContent']['error']['code']=='WORKFLOW_RETIRED'
    wait_for(lambda:any(d['id']==stack.device and d['online'] for d in stack.client.get('/api/devices').json()['devices']),timeout=20)


def test_old_browser_entry_is_readonly_archive(stack,chat_browser_pool,tmp_path):
    title='History-'+uuid.uuid4().hex[:8];receipt=archive(stack,title)
    page=chat_browser_pool('chromium').new_page(viewport={'width':1440,'height':1000})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(stack.url+'/#workflows');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
        expect(page.locator('#conversations-page')).to_be_visible()
        expect(page.locator('#page')).to_contain_text(title)
        page.locator('[data-archive-detail="'+receipt['workflow_id']+'"]').click()
        expect(page.locator('.modal')).to_contain_text('只读归档')
        expect(page.locator('.modal')).to_contain_text('Original recorded goal')
        assert not page.locator('[data-wf-action=update],[data-wf-action=create]').count()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/'archive-mobile.png'),full_page=True)
        assert not errors,errors
    finally:page.close()


def test_historical_workflow_reads_keep_exact_grant_isolation(stack):
    receipt=archive(stack)
    assert mcp(stack,'workflows_get',{'workflow_id':receipt['workflow_id']})['assigned_to_mcp']
    other=stack.must(stack.client.post('/api/grants',json={'label':'Other fixture','scopes':['read'],
        'projects':[stack.project['id']],'days':1}))
    denied=stack.mcp('project_query',{'operation':'workflow_get','options':{'workflow_id':receipt['workflow_id']}},token_value=other['token'])
    assert denied['isError'] and denied['structuredContent']['error']['code']=='WORKFLOW_NOT_FOUND'
