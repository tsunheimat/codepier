"""Execute the real panel observer: private receipt loss is not a network retry.

The submitted command is represented by one original operation id. This test
extracts the production function verbatim, not a second implementation, and only
substitutes its HTTP/read/time/UI boundaries. Browser command cases cover the
full page and real isolated Runner separately.
"""
import json
from pathlib import Path
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


def observe(case):
    script=r"""import fs from 'node:fs';import vm from 'node:vm';
const source=fs.readFileSync('web/app.js','utf8');
const begin=source.indexOf('async function settled('),end=source.indexOf('\nfunction download(',begin);
if(begin<0||end<begin)throw Error('Observer source boundary missing');
const input=CASE,calls=[],pauses=[],messages=[];let read=0,now=1;
const env={S:{session:{user_id:'fixture'}},location:{hash:'#spaces/fixture/vps'},
 Date:input.deadline?{now(){now+=3600000;return now;}}:Date,
 toast(value){messages.push(value);},networkState(value){messages.push(value);},stateNames:{failed:'Failed'},
 pause:async ms=>{pauses.push(ms);if(!input.transient)throw Error('Unexpected network retry');},
 sessionChanged:()=>Object.assign(Error('Changed'),{code:'SESSION_CHANGED'}),
 tool:async(name,args)=>{calls.push({name,args});read++;
  if(input.transient&&read===2)return {pending:false,state:'succeeded',result:{ok:true,data:{output:'original result'}}};
  throw Object.assign(Error('Original receipt not available'),{code:input.code,status:input.status});}};
vm.runInNewContext(source.slice(begin,end)+'\nthis.observe=settled;',env);
let result,error;
try{result=await env.observe(Promise.resolve({pending:true,operation_id:'original-command-id'}));}
catch(e){error={message:e.message,code:e.code,operation_id:e.operation_id,uncertain:e.operation_uncertain};}
console.log(JSON.stringify({result,error,calls,pauses}));
"""
    result=subprocess.run(['node','--input-type=module','-e',script.replace('CASE',json.dumps(case))],
        cwd=ROOT,text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('code,status',[
    ('NOT_FOUND',404),('ACCESS_DENIED',403),('SPACE_FORBIDDEN',403),
    ('INVALID_TOKEN',401),('CONNECTION_TARGET_CHANGED',409),('INVALID_ARGUMENTS',422),
    ('SESSION_CHANGED',None),
])
def test_authoritative_observation_denial_keeps_original_id_without_retry(code,status):
    result=observe({'code':code,'status':status})
    assert result['error']['code']==code
    assert result['error']['operation_id']=='original-command-id'
    assert result['error']['uncertain'] is True
    assert result['pauses']==[]
    assert result['calls']==[{'name':'operations_wait','args':{'operation_id':'original-command-id','wait_seconds':8}}]
    assert 'result' not in result


def test_transient_failure_only_retries_original_observation_never_executes_again():
    result=observe({'code':'NETWORK_UNCERTAIN','transient':True})
    assert result['result']=={'operation_id':'original-command-id','output':'original result'}
    assert 'error' not in result
    assert len(result['calls'])==2 and result['calls'][0]==result['calls'][1]
    assert result['calls'][0]['name']=='operations_wait'
    assert result['pauses']==[3000]


def test_observation_deadline_preserves_recovery_identity_and_uncertainty():
    result=observe({'deadline':True})
    assert result['error']['operation_id']=='original-command-id'
    assert result['error']['uncertain'] is True
    assert result['error']['code']=='OPERATION_OBSERVATION_INCOMPLETE'
    assert result['calls']==result['pauses']==[]


@pytest.mark.parametrize('change',['unchanged','closed','new_editor','navigation','login'])
def test_slow_original_receipt_cannot_reopen_or_replace_a_changed_view(change):
    script=r"""import fs from 'node:fs';import vm from 'node:vm';
const source=fs.readFileSync('web/app.js','utf8');
const start=source.indexOf('async function operationDetail('),end=source.indexOf('\nasync function consentModal(',start);
if(start<0||end<start)throw Error('Receipt reader source boundary missing');
const calls=[],shown=[];let resolve;
const env={S:{session:{user_id:'fixture'},modalIntent:1},location:{hash:'#spaces/fixture/vps'},
 api:path=>{calls.push(path);return new Promise(done=>resolve=done);},
 modal:(...args)=>shown.push(args),$:()=>({}),
 esc:value=>String(value),badge:value=>String(value),timeText:value=>String(value),
 json:value=>JSON.stringify(value),icon:()=>'',notice:value=>value};
vm.runInNewContext(source.slice(start,end)+'\nthis.reader=operationDetail;',env);
const pending=env.reader('original-operation');
if(CHANGE==='closed'||CHANGE==='new_editor')env.S.modalIntent++;
if(CHANGE==='navigation')env.location.hash='#spaces/another/profiles';
if(CHANGE==='login')env.S.session={user_id:'another'};
resolve({id:'original-operation',tool:'exec',actor:'private-fixture',state:'failed',
 result:{ok:false,data:{exit_code:7}},output:'ORIGINAL_PRIVATE_RESULT'});
await pending;console.log(JSON.stringify({calls,shown}));
"""
    completed=subprocess.run(['node','--input-type=module','-e',script.replace('CHANGE',json.dumps(change))],
        cwd=ROOT,text=True,capture_output=True,timeout=15)
    assert completed.returncode==0,completed.stdout+completed.stderr
    result=json.loads(completed.stdout)
    assert result['calls']==['/api/operations/original-operation']
    if change=='unchanged':
        assert len(result['shown'])==1 and result['shown'][0][0]=='操作详情 · exec'
        assert 'ORIGINAL_PRIVATE_RESULT' in result['shown'][0][1]
    else:
        assert result['shown']==[] and 'ORIGINAL_PRIVATE_RESULT' not in completed.stdout
