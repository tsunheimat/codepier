"""Old browser state is never imported as current preferences or authority.

Execute the production modules against disposable stores. Existing current-format
state still persists; corrupt or unsaved state cannot become a cached allow.
"""
from pathlib import Path
import json
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


def run_node(script):
    result=subprocess.run(['node','--input-type=module','-e',script],text=True,
        capture_output=True,timeout=20,cwd=ROOT)
    assert result.returncode==0,result.stdout+result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('disabled',[False,True])
def test_brand_metadata_never_reads_moves_or_deletes_pending_old_state(disabled):
    script="""import fs from 'node:fs';import vm from 'node:vm';
const values={'relay-appearance':'dark','relay-operation':'old-private-operation',
 'relay-task-submission':'old-private-request','codepier-appearance':'light'};
const before=JSON.stringify(values),calls=[];
const store={get length(){calls.push('length');return 0;},getItem(key){calls.push(key);return values[key];},
 setItem(key,v){calls.push('set');values[key]=v;},removeItem(key){calls.push('delete');delete values[key];}};
const window={};for(const name of ['localStorage','sessionStorage'])
 Object.defineProperty(window,name,{get(){calls.push(name);if(DISABLED)throw Error('blocked');return store;}});
vm.runInNewContext(fs.readFileSync('web/brand.js','utf8'),{window});
console.log(JSON.stringify({before,after:JSON.stringify(values),calls,brand:window.CodePierBrand,
 frozen:Object.isFrozen(window.CodePierBrand)}));"""
    result=run_node(script.replace('DISABLED','true' if disabled else 'false'))
    assert result['before']==result['after'] and result['calls']==[]
    assert result['brand']=={'name':'CodePier','title':'CodePier · 码头','tagline':'MCP 资源与客户端连接'}
    assert result['frozen']


def saved_state():
    return {'version':1,'profile_id':'a'*32,'origins':['https://example.test'],
        'pool':[{'tab_id':7,'window_id':4,'lease_id':'b'*32,'expires':9999999999999}],'seen':['c'*32]}


def test_extension_starts_empty_instead_of_adopting_old_permissions_tabs_and_leases():
    result=run_node("""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
const values={relayWorkspace:SAVED},reads=[],writes=[];
const local={async get(k){reads.push(k);return {[k]:values[k]};},
 async set(v){writes.push(v);Object.assign(values,v);},async remove(){throw Error('must not delete old data');}};
const one=await new BrowserWorkspace({storage:{local}}).load();
const two=await new BrowserWorkspace({storage:{local}}).load();
console.log(JSON.stringify({one,two,values,reads,writes:writes.length}));""".replace('SAVED',json.dumps(saved_state())))
    assert result['reads']==['codepierWorkspace','codepierWorkspace']
    assert result['one']==result['two']==result['values']['codepierWorkspace']
    assert result['one']['profile_id']!='a'*32 and len(result['one']['profile_id'])==32
    assert result['one']['origins']==result['one']['pool']==result['one']['seen']==[]
    assert result['values']['relayWorkspace']==saved_state()


def test_current_extension_state_preserves_its_explicit_identity_and_replay_ledger():
    result=run_node("""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
const values={codepierWorkspace:SAVED};const local={async get(k){return {[k]:values[k]};},
 async set(v){Object.assign(values,v);}};
const one=await new BrowserWorkspace({storage:{local}}).load();
const two=await new BrowserWorkspace({storage:{local}}).load();
console.log(JSON.stringify({one,two,values}));""".replace('SAVED',json.dumps(saved_state())))
    assert result=={'one':saved_state(),'two':saved_state(),'values':{'codepierWorkspace':saved_state()}}


@pytest.mark.parametrize('malformed',[{},[],False,2,{'version':2},
    {**saved_state(),'origins':'https://example.test'},
    {**saved_state(),'pool':{}},{**saved_state(),'seen':None},{**saved_state(),'profile_id':'invalid'}])
def test_invalid_state_is_never_overwritten_or_cached_after_a_rejected_load(malformed):
    result=run_node("""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
const values={codepierWorkspace:BAD};let reads=0,writes=0;
const local={async get(k){reads++;return {[k]:values[k]};},async set(){writes++;}};
const workspace=new BrowserWorkspace({storage:{local}}),errors=[];
for(let n=0;n<2;n++){try{await workspace.load();errors.push('unexpected-success');}catch(e){errors.push(e.code);}}
console.log(JSON.stringify({values,reads,writes,errors,cache:workspace.state}));""".replace('BAD',json.dumps(malformed)))
    assert result['errors']==['BROWSER_DOCUMENT_UNAVAILABLE']*2
    assert result['reads']==2 and result['writes']==0 and result['cache'] is None
    assert result['values']['codepierWorkspace']==malformed


def test_unsaved_extension_state_is_not_returned_as_a_cached_success():
    result=run_node("""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
let writes=0,reads=0;const local={async get(){reads++;return {};},async set(){writes++;throw Error('blocked');}};
const workspace=new BrowserWorkspace({storage:{local}}),errors=[];
for(let n=0;n<2;n++){try{await workspace.load();errors.push('unexpected-success');}catch(e){errors.push(e.message);}}
console.log(JSON.stringify({reads,writes,errors,cache:workspace.state}));""")
    assert result=={'reads':2,'writes':2,'errors':['blocked','blocked'],'cache':None}


def test_current_page_branding_does_not_advertise_a_migration_interface():
    source=(ROOT/'web/brand.js').read_text()
    assert 'migrateStorage' not in source and 'storageMigration' not in source
    html=(ROOT/'web/index.html').read_text()
    assert '<title>CodePier · 码头</title>' in html
    assert 'content="MCP 资源与客户端连接"' in html
