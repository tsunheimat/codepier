"""Real local Hub/Agent receipts survive removal of workflow compatibility."""
import uuid

from jsonschema import Draft202012Validator

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
    assert retired['isError'] and retired['structuredContent']['error']['code']=='INVALID_ARGUMENTS'


def test_real_command_failure_retains_true_exit_and_compact_receipt(stack):
    submitted=mcp(stack,'tasks_run',{'project':'Imago','task':'failure','idempotency_key':uuid.uuid4().hex})
    op=stack.poll(submitted['operation_id'])
    assert op['state']=='failed' and op['result']['data']['exit_code']==3
    assert not op['result']['data']['command_ok']
    compact=mcp(stack,'operations_get',{'operation_id':op['id'],'include_result':False,'include_output':False})
    assert 'output' not in compact and 'result' not in compact


def test_retired_calls_are_unavailable_after_actual_hub_restart_and_receipts_survive(stack):
    archive(stack, 'Inert restart history')
    submitted = mcp(stack, 'tasks_run', {'project': 'Imago', 'task': 'smoke', 'idempotency_key': uuid.uuid4().hex})
    completed = stack.poll(submitted['operation_id'])
    assert completed['state'] == 'succeeded'
    # Compare the same MCP projection on both sides of the restart; panel
    # receipts retain private next-call names that MCP presentation translates.
    before = mcp(stack, 'operations_get', {'operation_id': completed['id']})
    store = Store(stack.hubdir)
    try:
        history = {t: store.all('SELECT * FROM ' + t) for t in ('workflows', 'workflow_events', 'workflow_replays')}
        durable_result = store.one('SELECT result FROM operations WHERE id=?', (completed['id'],))
    finally: store.close()
    stack.hub.terminate(); stack.hub.wait(timeout=12); stack.start_hub()
    restored = mcp(stack, 'operations_get', {'operation_id': before['id']})
    assert restored['state'] == before['state'] == 'succeeded'
    assert restored['result'] == before['result']
    for name in ('workflows_list', 'workflows_get', 'workflows_create', 'workflows_update', 'workflows_handoff'):
        result = stack.mcp(name, {})
        assert result['isError'] and result['structuredContent']['error']['code'] == 'UNKNOWN_TOOL'
    from shared.mcp_protocol import MODERN, PREFIX, request_headers
    for name in ('workflows_list', 'workflows_get', 'workflows_create', 'workflows_update', 'workflows_handoff'):
        body = {'jsonrpc': '2.0', 'id': 'removed-tool', 'method': 'tools/call', 'params': {
            'name': name, 'arguments': {}, '_meta': {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}}}
        response = stack.client.post('/mcp', json=body, headers={
            'Authorization': 'Bearer ' + stack.pat, 'Accept': 'application/json, text/event-stream', **request_headers(body)})
        assert response.status_code == 400 and response.json()['error']['code'] == -32602
    for tool in ('workspace', 'project_query'):
        result = stack.mcp(tool, {'operation': 'handoff', 'options': {'workflow_id': history['workflows'][0]['id']}})
        assert result['isError'] and result['structuredContent']['error']['code'] == 'INVALID_ARGUMENTS'
    store = Store(stack.hubdir)
    try:
        assert {t: store.all('SELECT * FROM ' + t) for t in history} == history
        assert store.one('SELECT result FROM operations WHERE id=?', (completed['id'],)) == durable_result
    finally: store.close()
    wait_for(lambda: any(d['id'] == stack.device and d['online'] for d in stack.client.get('/api/devices').json()['devices']), timeout=20)
