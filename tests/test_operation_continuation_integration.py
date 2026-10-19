"""Real Connection-bound MCP continuation after a caller pauses or disconnects.

There is one public tools contract. Retired full/coding URL selectors and native
primitive aliases are not a test fixture or an application compatibility path.
"""
from __future__ import annotations

import shlex
import sys
import uuid

import httpx
import pytest
from jsonschema import Draft202012Validator

from shared.util import atomic_json
from tests.support import running_stack, wait_for


def command(counter, *, delay=2.5):
    return [sys.executable,'-u','-c',(
        'import pathlib,time; '
        f'p=pathlib.Path({counter!r}); '
        "p.open('a').write('once\\n'); "
        "print('CONTINUATION_STARTED',flush=True); "
        f'time.sleep({delay}); '
        "print('CONTINUATION_FINISHED',flush=True); "
        "p.with_suffix('.done').write_text('finished')")]


@pytest.fixture(scope='module')
def continuation_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('operation-continuation')) as stack:
        stack.stop_agent()
        stack.config['shell']={'enabled':True,'projects':['Imago'],'command':['/bin/sh','-c']}
        stack.config['tasks']['continuation']={'command':command('task.count'),'projects':['Imago'],'timeout':10}
        atomic_json(stack.config_path,stack.config);stack.start_agent()
        yield stack


def headers(stack):
    return {'Authorization':'Bearer '+stack.pat,'Accept':'application/json, text/event-stream',
        'MCP-Protocol-Version':'2025-11-25'}


def rpc(stack,method,params=None):
    response=stack.client.post('/mcp',headers=headers(stack),json={
        'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':method,'params':params or {}})
    assert response.status_code==200,response.text
    body=response.json();assert 'error' not in body,body
    return body['result']


@pytest.fixture(scope='module')
def catalog(continuation_stack):
    return {tool['name']:tool for tool in rpc(continuation_stack,'tools/list')['tools']}


def call(stack,catalog,name,arguments):
    definition=catalog[name]
    Draft202012Validator(definition['inputSchema']).validate(arguments)
    result=rpc(stack,'tools/call',{'name':name,'arguments':arguments})
    Draft202012Validator(definition['outputSchema']).validate(result['structuredContent'])
    return result


def follow(stack,catalog,continuation,**overrides):
    result=call(stack,catalog,continuation['name'],{**continuation['arguments'],**overrides})
    return {**result,'structuredContent':result['structuredContent']['operations'][0]}


@pytest.mark.parametrize('invocation',['command','task'])
def test_paused_polling_resumes_original_operation_once(continuation_stack,catalog,invocation):
    stack=continuation_stack;counter=invocation+'.count'
    args={'project':'Imago','idempotency_key':uuid.uuid4().hex,'yield_seconds':0}
    args.update({'task':'continuation'} if invocation=='task' else {'command':shlex.join(command(counter))})
    submitted=call(stack,catalog,'exec',args)
    assert not submitted['isError'],submitted
    receipt=submitted['structuredContent'];identifier=receipt['operation_id']
    assert receipt['pending'] and receipt['next_call']['name']=='task_query'
    assert receipt['next_call']['arguments']['operation_ids']==[identifier]
    pending=follow(stack,catalog,receipt['next_call'],wait_seconds=1)['structuredContent']
    assert pending['pending'] and pending['result'] is None
    assert pending['next_call']['arguments']['operation_ids']==[identifier]
    assert pending['next_call']['arguments']['after_output_seq']==pending['output_seq']
    # Leave no polling request open. Actual file effects prove execution does
    # not depend on a model keeping a connection or query loop alive.
    wait_for(lambda:(stack.imago/counter).with_suffix('.done').exists(),timeout=8)
    completed=follow(stack,catalog,pending['next_call'])
    assert not completed['isError'],completed
    terminal=completed['structuredContent']
    assert terminal['operation_id']==identifier and terminal['state']=='succeeded'
    assert not terminal['pending'] and terminal['next_call'] is None
    assert terminal['result']['data']['exit_code']==0
    assert 'CONTINUATION_FINISHED' in terminal['output']
    assert (stack.imago/counter).read_text()=='once\n'
    assert call(stack,catalog,'exec',args)['structuredContent']['operation_id']==identifier
    assert (stack.imago/counter).read_text()=='once\n'


def test_compact_terminal_response_recovers_failure_evidence(continuation_stack,catalog):
    stack=continuation_stack
    submitted=call(stack,catalog,'exec',{'project':'Imago','yield_seconds':0,
        'command':"printf 'EXPECTED_CONTINUATION_FAILURE\\n'; exit 7",'idempotency_key':uuid.uuid4().hex})
    assert not submitted['isError'],submitted
    receipt=submitted['structuredContent']
    compact=follow(stack,catalog,receipt['next_call'],include_output=False,include_result=False)['structuredContent']
    assert compact['state']=='failed' and not compact['pending']
    assert 'output' not in compact and 'result' not in compact
    assert compact['next_call']['name']=='task_query' and compact['next_call']['arguments']['operation']=='get'
    assert compact['next_call']['arguments']['operation_ids']==[receipt['operation_id']]
    assert 'after_output_seq' not in compact['next_call']['arguments']
    recovered=follow(stack,catalog,compact['next_call'])
    assert recovered['isError']
    terminal=recovered['structuredContent']
    assert terminal['state']=='failed' and terminal['next_call'] is None
    assert terminal['result']['data']['exit_code']==7 and terminal['result']['data']['command_ok'] is False
    assert 'EXPECTED_CONTINUATION_FAILURE' in terminal['output']
    assert 'EXPECTED_CONTINUATION_FAILURE' in terminal['result']['data']['output']


def test_http_wait_timeout_does_not_cancel_local_execution(continuation_stack,catalog):
    stack=continuation_stack;counter='disconnected-wait.count'
    receipt=call(stack,catalog,'exec',{'project':'Imago','yield_seconds':0,
        'command':shlex.join(command(counter)),'idempotency_key':uuid.uuid4().hex})['structuredContent']
    wait_for(lambda:(stack.imago/counter).exists(),timeout=5)
    continuation=receipt['next_call']
    with httpx.Client(base_url=stack.url,trust_env=False,timeout=httpx.Timeout(5,read=.2)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post('/mcp',headers=headers(stack),json={
                'jsonrpc':'2.0','id':2,'method':'tools/call','params':continuation})
    wait_for(lambda:(stack.imago/counter).with_suffix('.done').exists(),timeout=8)
    completed=follow(stack,catalog,continuation)
    assert not completed['isError']
    terminal=completed['structuredContent']
    assert terminal['operation_id']==receipt['operation_id'] and terminal['state']=='succeeded'
    assert terminal['result']['data']['exit_code']==0 and terminal['next_call'] is None
    assert (stack.imago/counter).read_text()=='once\n'
